#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""my2pg.py: MySQL to PostgreSQL database conversion

Copyright (c) 2010 Matrix Group International.
Licensed under the MIT license; see LICENSE file for terms.

"""
import os
import sys
import optparse
import logging
import re
import collections
import pickle

import MySQLdb
import psycopg2
from psycopg2 import InternalError
from MySQLdb.cursors import DictCursor, SSCursor

from psycopg2.extensions import adapt, register_adapter, AsIs


class GeometryText(object):
    def __init__(self, text):
        self.text = text


def adapt_geometry_text(geom):
    return AsIs("ST_GeomFromText(%s, 4326)" % adapt(geom.text))

register_adapter(GeometryText, adapt_geometry_text)

# We commit data rows every so often.
COMMIT_AFTER_ROWS = 10000
GEOMETRY_TYPES = (
    'linestring',
    'point',
    'polygon',
    'geometry',
    'multipolint',
    'multilinestring',
    'multipolygon',
    'geometrycollection',
)


def pg_execute(pg_conn, options, sql, args=()):
    """(Connection, Options, str, tuple)

    Log and execute a SQL command on the PostgreSQL connection.
    """

    if isinstance(sql, unicode):
        sql = sql.encode('utf-8')

    if not options.dry_run:
        pg_cur = pg_conn.cursor()
        try:
            pg_cur.execute(sql, args)
        except psycopg2.InterfaceError, msg:
            print "Error executing SQL: %s, data: %s" % (sql, str(args))
            raise psycopg2.InterfaceError, msg
        except psycopg2.InternalError as err:
            print "Error executing SQL: %s, data: %s" % (sql, str(args))
            raise err


def pg_execute_many(pg_conn, options, sql, args_list):
    """(Connection, Options, str, tuple)
    Log and execute a SQL command on the PostgreSQL connection.
    """
    if isinstance(sql, unicode):
        sql = sql.encode('utf-8')

    if not options.dry_run:
        pg_cur = pg_conn.cursor()
        try:
            pg_cur.executemany(sql, args_list)
        except Exception, msg:
            print "Error executing SQL: %s" % msg
            raise Exception(msg)

# XXX need to expand this set of words.
_reserved_words = set("""end user order group select""".split())


def is_reserved_word(word):
    """(str): bool

    Returns true if this word is a PostgreSQL reserved-word.
    """
    return word in _reserved_words


def fix_reserved_word(s):
    """(str): str

    Takes a MySQL name, and adds an underscore if it's a PostgreSQL
    reserved word.
    """
    if is_reserved_word(s.lower()):
        return '"%s"' % s
    return s


def convert_type(typ, auto_increment=False):
    """(str): str

    Parses a MySQL type declaration and returns the corresponding PostgreSQL
    type.
    """
    is_unsigned = typ.find('unsigned') != -1
    typ = typ.replace('unsigned', '').strip()
    new_type = ''
    if re.match('tinyint([(]\d+[)])?', typ):
        # MySQL tinyint is 1 byte, -128 to 127; we'll use the 2-byte int.
        new_type = 'smallint'
    elif re.match('smallint([(]\d+[)])?', typ):
        new_type = 'smallint'
    elif re.match('mediumint([(]\d+[)])?', typ):
        # MySQL medium int is 3 bytes; we'll use the 4-byte int.
        new_type = 'integer'
    elif re.match('bigint([(]\d+[)])?', typ):
        # XXX use the parametrized number?
        new_type = 'bigint'
    elif re.match('integer([(]\d+[)])?', typ):
        if is_unsigned:
            new_type = 'bigint'
        else:
            new_type = 'integer'
    elif re.match('int([(]\d+[)])?', typ):
        if is_unsigned:
            new_type = 'bigint'
        else:
            new_type = 'integer'
    elif typ == 'float':
        new_type = 'real'
    elif re.match('double([(]\d+,\d+[)])?', typ):
        new_type = 'double precision'
    elif typ == 'datetime':
        new_type = 'timestamp'
    elif typ == 'time':
        new_type = 'interval'
    elif typ in ('tinytext', 'text', 'mediumtext', 'longtext'):
        new_type = 'text'
    elif typ in ('tinyblob', 'blob', 'mediumblob', 'longblob'):
        new_type = 'bytea'
    elif typ in GEOMETRY_TYPES:
        new_type = 'geometry'
    elif typ.startswith('enum('):
        # For enums, we'll new_type =  a varchar long enough to hold
        # the longest possible value.
        # XXX this parsing is very dumb.
        # we assume that enum values does not contain commas
        values = typ[5:-1].replace("'", '').split(',')
        longest = max(len(v) for v in values)
        new_type = 'varchar(%i)' % longest

    if auto_increment and new_type in ('integer', 'bigint', 'smallint'):
        if new_type == 'bigint':
            new_type = 'bigserial'
        else:
            new_type = 'serial'
    # Give up and just return the input type.
    return new_type or typ


def convert_column_data(c):
    if c.type in GEOMETRY_TYPES:
        return 'asText(%s) as `%s`' % (c.name, c.name)
    elif c.type == 'date' and c.is_nullable:
        return "IF(%(p)s != '0000-00-00', DATE_FORMAT(%(p)s, '%%Y-%%m-%%d'), NULL) as `%(p)s`" % {'p': c.name}
    elif c.type == 'date' and not c.is_nullable:
        return "IF(%(p)s != '0000-00-00', DATE_FORMAT(%(p)s, '%%Y-%%m-%%d'), '1970-01-01') as `%(p)s`" % {'p': c.name}
    elif c.type in ('datetime', 'timestamp') and c.is_nullable:
        return "IF(%(p)s != '0000-00-00 00:00:00', DATE_FORMAT(%(p)s, '%%Y-%%m-%%d %%H:%%i:%%s'), NULL) as `%(p)s`" % {'p': c.name}
    elif c.type in ('datetime', 'timestamp') and not c.is_nullable:
        return "IF(%(p)s != '0000-00-00 00:00:00', DATE_FORMAT(%(p)s, '%%Y-%%m-%%d %%H:%%i:%%s'), '1970-01-01 00:00:00') as `%(p)s`" % {'p': c.name}
    else:
        return '`%s`' % c.name


def convert_data(type, data):
    """(Column, any) : any

    Convert a Python value retrieved from MySQL into a PostgreSQL value.
    """
    if type in ('tinyblob', 'blob', 'mediumblob', 'longblob') and data:
        # Convert to a BYTEA literal.  We just use octal escapes for
        # everything.
        return ''.join([('\\%03o') % ord(ch) for ch in data])
    if type in GEOMETRY_TYPES:
        return GeometryText(data)

    return data


class Column(object):
    """
    Represents a column.

    Instance attributes:
    name : str
    type : str
    position : int
    default : str
    is_nullable : bool
    auto_increment: bool

    """

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def pg_decl(self):
        """(): str

        Return the PostgreSQL declaration syntax for this column.
        """
        typ = convert_type(self.type, self.auto_increment)
        decl_typ = typ
        decl = '  "%s" %s' % (self.name, decl_typ)
        if self.default:
            default = self.get_default()
            decl += ' DEFAULT %s' % default
        if not self.is_nullable:
            decl += ' NOT NULL'
        return decl

    def get_default(self):
        if self.type.lower() in ('time', 'timestamp', 'datetime', 'date') and self.default:
            if self.default == 'CURRENT_TIMESTAMP':
                return 'now()'
            elif self.default == '0000-00-00 00:00:00':
                return "'1970-01-01 00:00:00'::timestamp"
            elif self.default == '0000-00-00':
                return "'1970-01-01'::date"
            else:
                return "'" + self.default + "'"
        typ = convert_type(self.type)
        if typ.startswith(('char', 'varchar')):
            return "'" + self.default + "'"

        return self.default


class Index(object):
    """
    Represents an index.

    Instance attributes:
    name : str
    table : str
    type : str
    column_names : [str]
    non_unique : bool
    nullable : bool

    """

    def __init__(self, **kw):
        self.column_names = []
        for k, v in kw.items():
            setattr(self, k, v)

    def pg_decl(self, schema='public'):
        """(): str

        Return the PostgreSQL declaration syntax for this index.
        """
        # We'll ignore the MySQL index name, and invent a new name.
        #name = 'idx_' + '_'.join([self.table] + self.column_names)
        name = self.name
        sql = 'CREATE INDEX %s ON "%s"."%s" (%s)' % (fix_reserved_word(name),
                                             schema,
                                             fix_reserved_word(self.table),
                                             ','.join(map(lambda x: '"%s"' % x, self.column_names)))
        if self.type:
            # XXX convert index_type:
            # BTREE, etc.
            pass
        return sql


def read_mysql_tables(mysql_cur, mysql_db, options):
    logging.info('Reading structure of MySQL database')
    mysql_cur.execute('''
        SELECT * FROM information_schema.tables
        WHERE table_schema = %s and TABLE_TYPE = 'BASE TABLE'
    ''', mysql_db)
    rows = mysql_cur.fetchall()
    tables = sorted(row['TABLE_NAME'] for row in rows)
    if options.starting_table:
        tables = [t for t in tables if options.starting_table <= t]

    # Convert tables
    table_cols = {}
    table_indexes = {}
    for table in tables:
        logging.debug('Reading table %s', table)
        mysql_cur.execute('''SELECT * FROM information_schema.columns
                          WHERE table_schema = %s and table_name = %s
                          ''', (mysql_db, table))
        cols = table_cols[table] = []
        for num, row in enumerate(mysql_cur.fetchall()):
            c = Column()
            cols.append(c)
            c.index = num
            c.name = row['COLUMN_NAME']
            c.type = row['COLUMN_TYPE']
            c.position = row['ORDINAL_POSITION']
            c.default = row['COLUMN_DEFAULT']
            c.is_nullable = bool(row['IS_NULLABLE'] == 'YES')
            c.auto_increment = row['EXTRA'] == 'auto_increment'
            # XXX character set?

        # Sort columns into left-to-right order.
        cols.sort(key=lambda c: c.position)

        # Convert indexes
        mysql_cur.execute('''SELECT * FROM information_schema.statistics
                          WHERE table_schema = %s AND table_name = %s
                          ''', (mysql_db, table))
        d = collections.defaultdict(Index)
        for row in mysql_cur.fetchall():
            index_name = row['INDEX_NAME']
            i = d[index_name]
            i.table = table
            i.name = index_name
            i.column_names.append(row['COLUMN_NAME'])
            i.type = row['INDEX_TYPE']
            i.non_unique = bool(row['NON_UNIQUE'])
            i.nullable = bool(row['NULLABLE'] == 'YES')
        table_indexes[table] = d.values()

    return tables, table_cols, table_indexes


def main():
    parser = optparse.OptionParser(
        '%prog [options] mysql-host mysql-db pg-host pg-db')
    parser.add_option('--data-only',
                      action="store_true", default=False,
                      dest="data_only",
                      help="Assume the tables already exist, and only convert data")
    parser.add_option('--drop-tables',
                      action="store_true", default=False,
                      dest="drop_tables",
                      help="Drop existing PostgreSQL tables (if any) before creating")
    parser.add_option('-n', '--dry-run',
                      action="store_true", default=False,
                      dest="dry_run",
                      help="Make no changes to PostgreSQL database")
    parser.add_option('--mysql-user',
                      action="store",
                      dest="mysql_user",
                      help="User for login if not current user.")
    parser.add_option('--mysql-password',
                      action="store",
                      dest="mysql_password",
                      help="Password to use when connecting to server.")
    parser.add_option('--pg-user',
                      action="store",
                      dest="pg_user",
                      help="User for login if not current user.")
    parser.add_option('--pg-schema',
                      action="store",
                      dest="pg_schema",
                      help="PostgreSQL database target schema (if not public).")
    parser.add_option('--pg-password',
                      action="store", default='',
                      dest="pg_password",
                      help="Password to use when connecting to server.")
    parser.add_option('--pickle=',
                      action="store", default='',
                      dest="pickle",
                      help="File for storing a pickled version of the MySQL table structure.")
    parser.add_option('--starting-table',
                      action="store", default=None,
                      dest="starting_table",
                      help="Name of table to start conversion with")
    parser.add_option('-v', '--verbose',
                      action="count", default=0,
                      dest="verbose",
                      help="Display more output as the script runs")

    options, args = parser.parse_args()
    if len(args) != 4:
        parser.print_help()
        sys.exit(1)

    mysql_host, mysql_db, pg_host, pg_db = args

    # Set logging level.
    if options.verbose:
        logging.basicConfig(level=logging.INFO)
        if options.verbose > 1:
            logging.basicConfig(level=logging.DEBUG)

    # Set up connections
    logging.info('Connecting to databases')

    mysql_conn = MySQLdb.Connection(
        user=options.mysql_user,
        passwd=options.mysql_password,
        db=mysql_db,
        host=mysql_host,
        use_unicode=True,
        charset='UTF8'
        )
    pg_conn = psycopg2.connect(
        database=pg_db,
        host=pg_host,
        user=options.pg_user,
        password=options.pg_password,
        )
    pg_conn.set_client_encoding('UNICODE')
    mysql_cur = mysql_conn.cursor(cursorclass=DictCursor)

    # Make list of tables to process.
    if options.pickle and os.path.exists(options.pickle):
        f = open(options.pickle, 'rb')
        tables, table_cols, table_indexes = pickle.load(f)
        f.close()

        # Discard tables that we don't need to process.
        if options.starting_table:
            tables = [t for t in tables if options.starting_table <= t]

    else:
        tables, table_cols, table_indexes = read_mysql_tables(mysql_cur,
                                                              mysql_db,
                                                              options)
        if options.pickle and not options.starting_table:
            f = open(options.pickle, 'wb')
            t = (tables, table_cols, table_indexes)
            pickle.dump(t, f)
            f.close()
    # Schema
    if not options.pg_schema:
        schema = "public"
    else:
        schema = options.pg_schema

    #
    # Convert the table structure.
    #
    if not options.data_only:
        for table in tables:
            cols = table_cols[table]
            indexes = table_indexes[table]

            # Drop table if necessary.
            if options.drop_tables:
                sql = '''DROP TABLE IF EXISTS "%s".%s''' % (schema, fix_reserved_word(table))
                pg_execute(pg_conn, options, sql)

            # Assemble into a PGSQL declaration
            pg_table = fix_reserved_word(table)
            sql = '''CREATE TABLE "%s".%s (\n''' % (schema, pg_table)
            std_columns = []
            geom_columns = []
            for c in cols:
                if convert_type(c.type.lower(), c.auto_increment) != 'geometry':
                    std_columns.append(c.pg_decl())
                else:
                    geom_column_def = "SELECT AddGeometryColumn('%s', '%s', '%s', 4326, '%s', 2)" % (schema, pg_table, c.name, c.type.upper())
                    geom_columns.append(geom_column_def)
            sql += ',\n'.join(std_columns) + '\n'

            # Look for index named PRIMARY, and add PRIMARY KEY if found.
            primary_L = [i for i in indexes if i.name == 'PRIMARY']
            if len(primary_L):
                if len(primary_L) > 1:
                    logging.warn('%s: Multiple PRIMARY indexes on table',
                                 table)
                else:
                    primary = primary_L.pop()
                    sql = sql.rstrip() + ',\n'
                    sql += '  PRIMARY KEY (%s)' % ','.join(map(lambda x: '"%s"' % x, primary.column_names))

            sql += ');'
            # Geometry columns
            sql += '\n' + '\n'.join(geom_columns) + ';'
            pg_execute(pg_conn, options, sql)

            # Create indexes
            for i in indexes:
                if i.name == 'PRIMARY':
                    continue

                sql = i.pg_decl(schema)
                try:
                    pg_execute(pg_conn, options, sql)
                except Exception:
                    logging.error('Failure creating index on table %s\n Statement: %s', table, sql,
                                  exc_info=True)

            pg_conn.commit()

    #
    # Convert data.
    #

    mysql_cur.close()
    mysql_cur = mysql_conn.cursor(cursorclass=SSCursor)

    logging.info('Converting data')
    for table in tables:
        # Convert data.
        logging.info('Converting data in table %s', table)
        pg_table = fix_reserved_word(table)
        cols = table_cols[table]

        # Assemble the INSERT statement once.
        ins_sql = ('INSERT INTO "%s".%s (%s) VALUES (%s);' %
                   (schema, pg_table,
                    ', '.join('"%s"' % c.name for c in cols),
                    ','.join(['%s'] * len(cols))))

        # Ensure the table is empty.
        pg_execute(pg_conn, options, 'DELETE FROM "%s".%s' % (schema, pg_table))

        mysql_cur.execute("SELECT %s FROM %s" % (', '.join(convert_column_data(c) for c in cols), table))

        # We don't do a fetchall() since the table contents are
        # very likely to not fit into memory.
        row_count = 0
        errors = 0

        while True:
            row = mysql_cur.fetchone()
            if row is None:
                break

            # Assemble a list of the output data that we'll subsequently
            # convert to a tuple.
            output_L = map(lambda x: convert_data(x.type, row[x.index]), cols)

            try:
                pg_execute(pg_conn, options, ins_sql, output_L)
            except InternalError, KeyboardInterrupt:
                raise
            except:
                logging.error('Failure inserting row into table %s', table,
                              exc_info=True)
                errors += 1
            else:
                row_count += 1
                if (row_count % COMMIT_AFTER_ROWS) == 0:
                    logging.info('Committing transaction after %i rows', row_count)
                    pg_conn.commit()

        logging.info("Table %s: %i rows converted (%i errors)",
                     table, row_count, errors)
        pg_conn.commit()

    # Update sequences to their table max values
    pg_execute(pg_conn, options, '''
        CREATE OR REPLACE FUNCTION sequence_max_value(oid) RETURNS bigint
        VOLATILE STRICT LANGUAGE plpgsql AS  $$
        DECLARE
         tabrelid oid;
         colname name;
         r record;
         newmax bigint;
        BEGIN
         FOR tabrelid, colname IN SELECT attrelid, attname
                       FROM pg_attribute
                      WHERE (attrelid, attnum) IN (
                              SELECT adrelid::regclass,adnum
                                FROM pg_attrdef
                               WHERE oid IN (SELECT objid
                                               FROM pg_depend
                                              WHERE refobjid = $1
                                                    AND classid = 'pg_attrdef'::regclass
                                            )
                  ) LOOP
              FOR r IN EXECUTE 'SELECT max(' || quote_ident(colname) || ') FROM ' || tabrelid::regclass LOOP
                  IF newmax IS NULL OR r.max > newmax THEN
                      newmax := r.max;
                  END IF;
              END LOOP;
          END LOOP;
          RETURN newmax;
        END; $$ ;
    ''')
    pg_execute(pg_conn, options, '''
        SELECT relname, setval(oid, sequence_max_value(oid))
        FROM pg_class
        WHERE relkind = 'S';
    ''')

    # Close connections
    logging.info('Closing database connections')
    mysql_conn.close()
    pg_conn.close()


if __name__ == '__main__':
    main()
