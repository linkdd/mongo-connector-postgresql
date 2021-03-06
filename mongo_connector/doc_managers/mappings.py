# coding: utf8

from future.utils import iteritems, PY2, PY3
from RestrictedPython.Guards import safe_builtins
from RestrictedPython import compile_restricted

from mongo_connector.doc_managers.formatters import DocumentFlattener
from mongo_connector.doc_managers.utils import (
    db_and_collection,
    ARRAY_TYPE,
    ARRAY_OF_SCALARS_TYPE
)
from mongo_connector.doc_managers.mapping_schema import MAPPING_SCHEMA
from mongo_connector.errors import InvalidConfiguration

from importlib import import_module
import jsonschema
import logging

logging.basicConfig()
LOG = logging.getLogger(__name__)

_formatter = DocumentFlattener()


def _clean_and_flatten_doc(mappings, doc, namespace):
    """Reformats the given document before insertion into Solr.
    This method reformats the document in the following ways:
      - removes extraneous fields that aren't defined in schema.xml
      - unwinds arrays in order to find and later flatten sub-documents
      - flattens the document so that there are no sub-documents, and every
        value is associated with its dot-separated path of keys
      - inserts namespace and timestamp metadata into the document in order
        to handle rollbacks
    An example:
      {"a": 2,
       "b": {
         "c": {
           "d": 5
         }
       },
       "e": [6, 7, 8]
      }
    becomes:
      {"a": 2, "b.c.d": 5, "e.0": 6, "e.1": 7, "e.2": 8}
    """

    # PGSQL cannot index fields within sub-documents, so flatten documents
    # with the dot-separated path to each value as the respective key
    flat_doc = _formatter.format_document(doc)

    # Extract column names and mappings for this table
    db, coll = db_and_collection(namespace)
    if db in mappings:
        mappings_db = mappings[db]
        if coll in mappings_db:
            mappings_coll = mappings_db[coll]

            # Only include fields that are explicitly provided in the schema
            def include_field(field):
                return field in mappings_coll

            return dict((k, v) for k, v in flat_doc.items() if include_field(k))
    return {}


def get_mapped_document(mappings, document, namespace):
    cleaned_and_flatten_document = _clean_and_flatten_doc(mappings, document, namespace)

    db, collection = db_and_collection(namespace)
    keys = list(cleaned_and_flatten_document)

    for key in keys:
        field_mapping = mappings[db][collection][key]

        if 'dest' in field_mapping:
            mappedKey = field_mapping['dest']
            cleaned_and_flatten_document[mappedKey] = cleaned_and_flatten_document.pop(key)

    return cleaned_and_flatten_document


def get_mapped_field(mappings, namespace, field_name):
    db, collection = db_and_collection(namespace)
    return mappings[db][collection][field_name]['dest']


def get_primary_key(mappings, namespace):
    db, collection = db_and_collection(namespace)
    return mappings[db][collection]['pk']


def get_transformed_value(mapped_field, mapped_document, key):
    val = mapped_document[key]

    if 'transform' in mapped_field:
        transform = mapped_field['transform']

        if transform[0] == '@':
            transform_path = transform[1:].rsplit('.', 1)
            module_path = 'mongo_connector.doc_managers.transforms'

            if len(transform_path) == 2:
                module_path, transform_path = transform_path

            else:
                transform_path = transform_path[0]

            try:
                module = import_module(module_path)
                transform = getattr(module, transform_path)

            except (ImportError, ValueError) as err:
                LOG.error(
                    'Impossible to use transform function: {0}'.format(err)
                )
                transform = None

        else:
            try:
                src = 'transform = lambda val: {0}'.format(transform)
                restricted_globals = {
                    '__builtin__': safe_builtins
                }
                restricted_locals = {}
                code = compile_restricted(src, '<string>', 'exec')

                if PY2:
                    exec(code) in restricted_globals, restricted_locals

                elif PY3:
                    exec(code, restricted_globals, restricted_locals)

                transform = restricted_locals['transform']

            except Exception as err:
                LOG.error(
                    'Impossible to use transform code: {0}'.format(err)
                )
                transform = None

        if transform is not None:
            try:
                new_val = transform(val)

            except Exception as err:
                LOG.error(
                    'An error occured during field transformation: {0}'.format(
                        err
                    )
                )

            else:
                val = new_val

    return val


def get_transformed_document(mappings, db, collection, mapped_document):
    mapped_fields = {
        mapping['dest']: mapping
        for _, mapping in iteritems(mappings[db][collection])
        if 'dest' in mapping and mapping['type'] not in (
            ARRAY_TYPE,
            ARRAY_OF_SCALARS_TYPE
        )
    }
    keys = list(mapped_fields.keys())
    keys.sort()

    return {
        key: get_transformed_value(
            mapped_fields[key],
            mapped_document, key
        ) if key in mapped_fields else mapped_document[key]
        for key in mapped_document
    }


def is_mapped(mappings, namespace, field_name=None):
    db, collection = db_and_collection(namespace)
    return db in mappings and collection in mappings[db] and \
           (field_name is None or field_name in mappings[db][collection])


def is_id_autogenerated(mappings, namespace):
    primary_key = get_primary_key(mappings, namespace)

    db, collection = db_and_collection(namespace)
    mapped_to_primary_key = [k for k, v in iteritems(mappings[db][collection]) if
                             'dest' in v and v['dest'] == primary_key]
    return len(mapped_to_primary_key) == 0


def get_scalar_array_fields(mappings, db, collection):
    if db not in mappings or collection not in mappings[db]:
        return []

    return [
        k for k, v in iteritems(mappings[db][collection])
        if 'type' in v and v['type'] == ARRAY_OF_SCALARS_TYPE
        ]


def validate_mapping(mappings):
    try:
        jsonschema.validate(mappings, MAPPING_SCHEMA)

    except jsonschema.ValidationError as err:
        raise InvalidConfiguration(
            "Supplied mapping file is invalid: {0}".format(err)
        )

    # Integrity check
    ARRAYS_TYPE = [ARRAY_TYPE, ARRAY_OF_SCALARS_TYPE]

    for database in mappings:
        dbmapping = mappings[database]

        for collection in dbmapping:
            mapping = dbmapping[collection]

            if mapping['pk'] not in mapping:
                # look for a linked table
                for linked_collection in dbmapping:
                    if linked_collection != collection:
                        linked_mapping = dbmapping[linked_collection]
                        links = [
                            True
                            for field in linked_mapping
                            if field != 'pk'
                            and linked_mapping[field]['type'] in ARRAYS_TYPE
                            and linked_mapping[field]['dest'] == collection
                        ]

                        if len(links) > 0:
                            break

                else:
                    # No linked table found, cannot generate primary key
                    raise InvalidConfiguration(
                        "Primary key {0} mapping not found in {1}.{2}".format(
                            mapping['pk'],
                            database,
                            collection
                        )
                    )

            for fieldname in mapping:
                if fieldname != 'pk':
                    field = mapping[fieldname]
                    ftype = field['type']

                    if ftype in ARRAYS_TYPE:
                        dest = field['dest']

                        # Check for linked table presence
                        if dest not in dbmapping:
                            raise InvalidConfiguration(
                                "Collection {0} mapping not found in {1}".format(
                                    dest,
                                    database
                                )
                            )

                        # Check for foreign key presence in linked table
                        elif field['fk'] not in dbmapping[dest]:
                            raise InvalidConfiguration(
                                "Foreign key {0} mapping not found in {1}.{2}".format(
                                    field['fk'],
                                    database,
                                    dest
                                )
                            )

                        else:
                            fk = dbmapping[dest][field['fk']]
                            pk = mapping.get(mapping['pk'], {'type': 'SERIAL'})

                            # Check for foreign key and linked table's primary key types
                            if fk['type'] != pk['type']:
                                raise InvalidConfiguration(
                                    "Foreign key {0}.{1}.{2} type mismatch with primary key {0}.{3}.{4}".format(
                                        database,
                                        dest,
                                        field['fk'],
                                        collection,
                                        mapping['pk']
                                    )
                                )

                        # Check for value field presence in linked table
                        if ftype == ARRAY_OF_SCALARS_TYPE:
                            valuefield = field['valueField']

                            if valuefield not in dbmapping[dest]:
                                raise InvalidConfiguration(
                                    'Value field {0}.{1}.{2} not mapped in {0}.{3}'.format(
                                        database,
                                        collection,
                                        valuefield,
                                        dest
                                    )
                                )
