"""Actuates the dispenser by setting the desired state of the shadow"""

# TODO: Add error handling to return 500 with CORS set

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
    """Return count and color based on credit float amount

    the ring LED has 8 elements and are set based on credit amounts:

    credits         LEDs lit            Color
    =============== =================== ===========================
    0 - 0.24        8                    red (#FF0000)
    0.25 - 0.49     2                    white (#F0F0F0)
    0.50 - 0.74     4                    white (#F0F0F0)
    0.75 - 0.99     6                    white (#F0F0F0)
    1.00 - 1.99     1                    darkest green (#006600)
    2.00 - 2.99     2                    dark green (#009900)
    3.00 - 3.99     3                    green (#00E600)
    4.00 - 4.99     4                    bright green(#00FF00)
    5.00 - 5.99     5                    bright green(#00FF00)
    6.00 - 6.99     6                    bright green(#00FF00)
    7.00 - 7.99     7                    bright green(#00FF00)
    8.00 >          8                    bright green(#00FF00)
    """

    # Green colors for 1, 2, 3, and greater
    color_scale = ["#006600", "#009900", "#00E600", "#00FF00"]
    # Cast to float, most likely Decimal coming in
    amount = float(amount)
    if amount == 0.0:
        # No credits all lit up red
        count = 8
        color = "#FF0000"
    elif amount < 1.00:
        # 0.01 - 0.99 - 2 per .25, white
        count = int(amount / 0.25) * 2
        color = "#F0F0F0"
    else:
        # At least 1.00 or more, set count to 1-8 per 1.00
        count = int(amount / 1.00) if amount < 8.00 else 8
        if count < 4:
            color = color_scale[count - 1]
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


def get_request_id():
    """Generates nnnn-nnnn random string, zero padded"""
    return f"{randint(0,9999):04d}-{randint(0,9999):04d}"


def request_details(requests, command):
    """Return details for command in requests"""

    for request in requests:
        # Parse record format of "requestId|command|timestamp|target"
        record = request.split("|")
        if record[1] == command:
            return {
                "requestId": record[0],
                "command": record[1],
                "timestamp": float(record[2]),
            }
    # No matching requestId for command found
    return None


def remove_request(record, command):
    """Return record with request for "command" removed"""

    requests = record["requests"]
    new_requests = []
    for i in requests:
        if i.split("|")[1] != command:
            new_requests.append(i)
    record["requests"] = new_requests
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
                # Get current dispenser record and validate credits and request status
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
                dispense_request = request_details(
                    requests=dispenser_record["requests"], command="dispense"
                )
                if dispense_request is not None:
                    if (time.time() - dispense_request["timestamp"]) < 5:
                        # request still current
                        log_event(
                            event_table,
                            dispenser,
                            f'Dispense: ERROR: request {dispense_request["requestId"]} already in progress',
                        )
                        return http_response(
                            httpHeaders,
                            200,
                            "Dispense operation already in progress, no action taken",
                        )
                    else:
                        # Stale request, remove from request list and continue
                        dispenser_record = remove_request(
                            record=dispenser_record, command="dispense"
                        )
                # All request checks completed
                # No in-flight requests, create new one and append to requests
                request_id = get_request_id()
                dispenser_record["requests"].append(
                    f"{request_id}|dispense|{time.time()}|dispenser"
                )
                write_dispenser_record(dispenser_record, dispenser_table)
                message = {
                    "state": {
                        "desired": {
                            "request": {
                                "command": "dispense",
                                "requestId": request_id,
                                "timestamp": time.time(),
                            }
                        }
                    }
                }

                # Read shadow and add/replace request to desired state
                iot_client.update_thing_shadow(
                    thingName=dispenser, payload=json.dumps(message)
                )

                # iot_client.publish(
                #     topic=f"cmd/{dispenser}", qos=0, payload=json.dumps(message)
                # )
                log_event(
                    event_table,
                    dispenser,
                    f"Dispense: Successful request to dispense initiated, requestId: {request_id}",
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
    """Process event sent via IoT Rules Engine action, this originates from the dispenser,
       and the event comes from the topic $aws/things/NNN/shadow/update/accepted

        If the value state.reported.response is found in the shadow event, reconcile
        and clear the value out.
    """

    dispenser = event["topic"].split("/")[2]

    try:
        if event["state"]["reported"].get("response") is not None:
            event_request_id = event["state"]["reported"]["response"]["requestId"]
        else:
            # Response object found but NULL, ignore as this is us clearing out the
            # object below
            # TODO - add logic to IoT rule to not trigger is value is null
            logger.info("Response object found but set to null, this is a follow-up shadow update to clear, disregarding")
            return
    except KeyError:
        # No response object found, shadow event not of interest
        return
    try:
        # Check for corresponding requestId in event from DynamoDB table
        event_response_result = event["state"]["reported"]["response"]["result"]
        dispenser_record = read_dispenser(dispenser, dispenser_table)
        dispense_request = request_details(
            requests=dispenser_record["requests"], command="dispense"
        )
        logger.info(f"got request from shadow and DDB, shadow: {event}, DDB: {dispenser_record}")
        if dispense_request is not None:
            if (event_request_id == dispense_request["requestId"]) and (
                event_response_result == "success"
            ):
                # request still current - delete request, deduct $1.00 from  dispenser, and log
                dispenser_record = remove_request(
                    record=dispenser_record, command="dispense"
                )
                dispenser_record["credits"] = dispenser_record["credits"] - Decimal(
                    1.00
                )
                write_dispenser_record(dispenser_record, dispenser_table)
                # Set ring LED with new state (if needed) and clear out request and response objects
                count, color = set_led_ring(dispenser_record["credits"])
                new_state = {
                    "state": {
                        "desired": {"led_ring": {"count": count, "color": color}},
                        "reported": {"response": None},
                    }
                }
                iot_client.update_thing_shadow(
                    thingName=dispenser, payload=json.dumps(new_state)
                )

                # Place on events topic to trigger app to refresh - will also generate log entry
                iot_publish_event(
                    topic=f"events/{dispenser}",
                    message=(
                        f"Dispense: Successfully dispensed for request "
                        f'{dispense_request["requestId"]} after '
                        f'{(time.time() - dispense_request["timestamp"]):0.2f} seconds, '
                        f"$1.00 deducted from credits",
                    ),
                )
            elif (event_request_id == dispense_request["requestId"]) and (
                event_response_result == "failure"
            ):
                # dispenser reporting error, remove dispense entry but do not deduct credits
                dispenser_record = remove_request(
                    record=dispenser_record, command="dispense"
                )
                write_dispenser_record(dispenser_record, dispenser_table)

                # Clean out any request/response objects, no need to touch rest of dispenser
                new_state = {
                    "state": {
                        "desired": {"request": None},
                        "reported": {"response": None},
                    }
                }
                iot_client.update_thing_shadow(
                    thingName=dispenser, payload=json.dumps(new_state)
                )

                # Log entry directly to event table, no change to state
                log_event(
                    event_table,
                    dispenser,
                    f"Dispense: ERROR, did not dispense for request "
                    f'{dispense_request["requestId"]} after '
                    f'{(time.time() - dispense_request["timestamp"]):0.2f} seconds, '
                    f"dispenser reported failure. No credits deducted",
                )
            else:
                # requestId does not match, clear and do not deduct
                dispenser_record = remove_request(
                    record=dispenser_record, command="dispense"
                )
                write_dispenser_record(dispenser_record, dispenser_table)
                log_event(
                    event_table,
                    dispenser,
                    f'Dispense: ERROR, dispenser requestId {event["requestId"]} '
                    f'does not match stored request {dispense_request["requestId"]}, '
                    f"reset request state and NO credits deducted",
                )
        else:
            # Should not get here normally, discard response and log
            log_event(
                event_table,
                dispenser,
                f'Dispense: ERROR, requestId: {event["requestId"]} not found '
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
