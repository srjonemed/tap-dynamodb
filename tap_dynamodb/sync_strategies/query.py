import time

import singer
from singer import metadata
from tap_dynamodb import dynamodb
from tap_dynamodb.deserialize import Deserializer

LOGGER = singer.get_logger()


def query_table(
    table_name: str,
    index_name: str,
    key_condition_expression: str,
    expression_attribution_values: dict,
    config: dict = None
):
    query_params = {
        'TableName': table_name,
        'IndexName': index_name,
        'Select': 'ALL_ATTRIBUTES',
        'Limit': 1000,
        'KeyConditionExpression': key_condition_expression,
        'ExpressionAttributeValues': expression_attribution_values
    }

    client = dynamodb.get_client(config)
    has_more = True

    while has_more:
        LOGGER.info('Querying table %s with params:', table_name)
        for key, value in query_params.items():
            LOGGER.info('\t%s = %s', key, value)

        result = client.query(**query_params)
        yield result

        if result.get('LastEvaluatedKey'):
            query_params['ExclusiveStartKey'] = result['LastEvaluatedKey']

        has_more = result.get('LastEvaluatedKey', False)


def sync_query(config, state, stream):
    table_name = stream['tap_stream_id']

    #before writing the table version to state, check if we had one to begin with
    first_run = singer.get_bookmark(state, table_name, 'version') is None

    # last run was interrupted if there is a last_id_fetched bookmark
    was_interrupted = singer.get_bookmark(state,
                                          table_name,
                                          'last_evaluated_key') is not None

    #pick a new table version if last run wasn't interrupted
    if was_interrupted:
        stream_version = singer.get_bookmark(state, table_name, 'version')
    else:
        stream_version = int(time.time() * 1000)

    state = singer.write_bookmark(state,
                                  table_name,
                                  'version',
                                  stream_version)
    singer.write_state(state)

    # For the initial replication, emit an ACTIVATE_VERSION message
    # at the beginning so the records show up right away.
    if first_run:
        singer.write_version(table_name, stream_version)

    mdata = metadata.to_map(stream['metadata'])
    index_name = metadata.get(mdata, (), "IndexName")
    key_condition_expression = metadata.get(mdata, (), "KeyConditionExpression")
    expression_attribute_values = metadata.get(mdata, (), "ExpressionAttributeValues")

    rows_saved = 0

    deserializer = Deserializer()
    for result in query_table(table_name, index_name, key_condition_expression, expression_attribute_values, config):
        for item in result.get('Items', []):
            rows_saved += 1
            # TODO: Do we actually have to put the item we retreive from
            # dynamo into a map before we can deserialize?
            record = deserializer.deserialize_item(item)
            record_message = singer.RecordMessage(stream=table_name,
                                                  record=record,
                                                  version=stream_version)

            singer.write_message(record_message)
        if result.get('LastEvaluatedKey'):
            state = singer.write_bookmark(state, table_name, 'last_evaluated_key', result.get('LastEvaluatedKey'))
            singer.write_state(state)

    state = singer.clear_bookmark(state, table_name, 'last_evaluated_key')

    state = singer.write_bookmark(state,
                                  table_name,
                                  'initial_full_table_complete',
                                  True)

    singer.write_state(state)

    singer.write_version(table_name, stream_version)

    return rows_saved
