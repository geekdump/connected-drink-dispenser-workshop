"""Actuates the dispenser by setting the desired state of the shadow"""

import json
import os
import logging
import time
import boto3
from datetime import datetime as dt
from decimal import Decimal
from random import randint
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError

__copyright__ = (
    "Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved."
)
__license__ = "MIT-0"

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Global Variables
httpHeaders = {"Access-Control-Allow-Origin": "*"}
color_scale = ["#FF8000", "#666600", "#33FF33", "#66B2FF"]

ddb = boto3.resource("dynamodb")
dispenser_table = ddb.Table(os.environ["DISPENSER_TABLE"])
event_table = ddb.Table(os.environ["EVENT_TABLE"])
iot_client = boto3.client("iot-data")


def http_response(headers, status_code, body):
    """Create response dict for returning query"""
    if type(body) != str:
        if type(body) == dict:
            body = json.dumps(body)
        else:
            body = "ERROR, invalid type of {} for body of return".format(type(body))
            status_code = 500
    return {"body": body, "headers": headers, "statusCode": status_code}


def set_led_ring(amount: float):
    """Return count and color based on float amount"""

    # Cast to float, most likely Decimal coming in
    amount = float(amount)
    if amount < 1.00:
        count = int(amount / 0.25)
        color = "#FF0000"
    else:
        count = 5
        scale = int(amount / 1)
        if scale < 4:
            color = color_scale[scale - 1]
        else:
            color = color_scale[3]
    return count, color


def log_event(table, dispenser_id, message):
    """Put log entry into DynamoDB table"""

    log_entry = {
        "dispenserId": dispenser_id,
        "timestamp": dt.utcnow().isoformat() + "Z",
        "log": message,
    }
    try:
        # Write to events table
        table.put_item(Item=log_entry)
    except ClientError as e:
        logging.error("An error has occurred:, {}".format(e))


def read_dispenser(dispenser, table):
    """Read, parse, and return dispenser record"""

    response = table.query(KeyConditionExpression=Key("dispenserId").eq(dispenser))
    # If count is zero, this is a non-existent dispenser
    if response["Count"] == 0:
        return http_response(
            httpHeaders, 200, f"ERROR: Dispenser {dispenser} does not exist"
        )
    else:
        return response["Items"][0]


def write_dispenser_record(dispenser_record, dispenser_table):
    """Replace existing dispenser record"""

    try:
        # Write to dispenser table
        dispenser_table.put_item(Item=dispenser_record)
    except ClientError as e:
        logging.error("An error has occurred:, {}".format(e))


def get_session_id():
    """Generates nnnn-nnnn random string, zero padded"""
    return f"{randint(0,9999):04d}-{randint(0,9999):04d}"


def session_details(sessions, command):
    """Return details for command in sessions"""

    for session in sessions:
        # Parse record format of "sessionId|command|timestamp|target"
        record = session.split("|")
        if record[1] == command:
            return {
                "sessionId": record[0],
                "command": record[1],
                "timestamp": float(record[2]),
            }
    # No matching sessionId for command found
    return None


def remove_session(record, command):
    """Return record with session for "command" removed"""

    sessions = record["sessions"]
    new_sessions = []
    for i in sessions:
        if i.split("|")[1] != command:
            new_sessions.append(i)
    record["sessions"] = new_sessions
    return record


def iot_publish_event(topic, message):
    """Publish message to events topic"""

    iot_client.publish(topic=topic, payload=json.dumps({"message": message}))


def process_api_event(event, dispenser_table, event_table):
    """Process dispense REQUEST based on API Gateway claims and query parameters"""
    try:
        # Id of dispenser (app) that invoked the Lambda
        dispenser = str(
            event["requestContext"]["authorizer"]["claims"]["custom:dispenserId"]
        )
        params = event["queryStringParameters"]
        print(f"Params: {params}, dispenser: {dispenser}")
        if "dispenserId" in params:
            if params["dispenserId"] == dispenser:
                # Get current dispenser record and validate credits and session status
                dispenser_record = read_dispenser(dispenser, dispenser_table)
                if dispenser_record["credits"] < 1.00:
                    # Not enough to dispense
                    log_event(
                        event_table,
                        dispenser,
                        f"Dispense: ERROR: dispenser: {dispenser} only has "
                        f'${dispenser_record["credits"]:0.2f} credits, at '
                        f"least $1.00 required to activate dispenser",
                    )
                    return http_response(
                        httpHeaders,
                        200,
                        f'Dispenser: {dispenser} only has ${dispenser_record["credits"]:0.2f} '
                        f"credits, at least $1.00 required to activate dispenser",
                    )
                dispense_session = session_details(
                    sessions=dispenser_record["sessions"], command="dispense"
                )
                if dispense_session is not None:
                    if (time.time() - dispense_session["timestamp"]) < 5:
                        # Session still current
                        log_event(
                            event_table,
                            dispenser,
                            f'Dispense: ERROR: session {dispense_session["sessionId"]} already in progress',
                        )
                        return http_response(
                            httpHeaders,
                            200,
                            "Dispense operation already in progress, no action taken",
                        )
                    else:
                        # Stale session, remove from session list and continue
                        dispenser_record = remove_session(
                            record=dispenser_record, command="dispense"
                        )
                # All session checks completed
                # No in-flight sessions, create new one and append to sessions
                session_id = get_session_id()
                dispenser_record["sessions"].append(
                    f"{session_id}|dispense|{time.time()}|dispenser"
                )
                write_dispenser_record(dispenser_record, dispenser_table)
                message = {
                    "command": "dispense",
                    "sessionId": session_id,
                    "timestamp": time.time(),
                }
                iot_client.publish(
                    topic=f"cmd/{dispenser}", qos=0, payload=json.dumps(message)
                )
                log_event(
                    event_table,
                    dispenser,
                    f"Dispense: Successful request to dispense initiated, sessionId: {session_id}",
                )
                return http_response(
                    httpHeaders, 200, f"Dispenser {dispenser} requested to be activated"
                )
            else:
                return http_response(
                    httpHeaders,
                    500,
                    "dispenser parameter must match users assigned dispenser -- no cheating!!!",
                )
        else:
            return http_response(
                httpHeaders, 500, 'Parameter "dispenserId" must be present'
            )
    except KeyError as e:
        logger.error("Error: %s", e)
        return http_response(httpHeaders, 500, e)


def process_iot_event(event, dispenser_table, event_table):
    """Process event sent via IoT Rules Engine action, this originates from the dispenser"""

    dispenser = event["topic"].split("/")[1]
    try:
        # Check for corresponding sessionId in event from DynamoDB table
        dispenser_record = read_dispenser(dispenser, dispenser_table)
        dispense_session = session_details(
            sessions=dispenser_record["sessions"], command="dispense"
        )
        if dispense_session is not None:
            if event["sessionId"] == dispense_session["sessionId"]:
                # Session still current - delete session, deduct $1.00 from  dispenser, and log
                dispenser_record = remove_session(
                    record=dispenser_record, command="dispense"
                )
                dispenser_record["credits"] = dispenser_record["credits"] - Decimal(
                    1.00
                )
                write_dispenser_record(dispenser_record, dispenser_table)
                # Set ring LED with new state (if needed)
                count, color = set_led_ring(dispenser_record["credits"])
                desired_state = {
                    "state": {"desired": {"led_ring": {"count": count, "color": color}}}
                }
                iot_client.update_thing_shadow(
                    thingName=dispenser, payload=json.dumps(desired_state)
                )



                # Place on events topic to trigger app to refresh - will also generate log entry
                iot_publish_event(
                    topic=f"events/{dispenser}",
                    message=(
                        f"Dispense: Successfully dispensed for session "
                        f'{dispense_session["sessionId"]} after '
                        f'{(time.time() - dispense_session["timestamp"]):0.2f} seconds, '
                        f"$1.00 deducted from credits",
                    ),
                )
            else:
                # sessionId does not match, clear and do not deduct
                dispenser_record = remove_session(
                    record=dispenser_record, command="dispense"
                )
                write_dispenser_record(dispenser_record, dispenser_table)
                log_event(
                    event_table,
                    dispenser,
                    f'Dispense: ERROR, dispenser sessionID {event["sessionId"]} '
                    f'does not match stored session {dispense_session["sessionId"]}, '
                    f"reset session state and NO credits deducted",
                )
        else:
            # Should not get here normally, discard response and log
            log_event(
                event_table,
                dispenser,
                f'Dispense: ERROR, sessionId: {event["sessionId"]} not found '
                f"in Dispenser database, no action taken",
            )
    except KeyError as e:
        logger.error("Error: %s", e)
    return


def handler(event, context):
    """Dispense drink if credits are available or reconcile outstanding operations"""
    logger.info("Received event: {}".format(json.dumps(event)))

    if "queryStringParameters" in event:
        # Invoked by API Gateway, should only have one parameter
        if event["queryStringParameters"] is not None:
            response = process_api_event(
                event=event, dispenser_table=dispenser_table, event_table=event_table
            )
        else:
            response = http_response(httpHeaders, 500, "No parameters provided")
        return response
    else:
        # Invoked by IoT Rules engine, event contains message payload
        process_iot_event(
            event=event, dispenser_table=dispenser_table, event_table=event_table
        )
        return