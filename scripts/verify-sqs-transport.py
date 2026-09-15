"""Prove local SQS visibility retries and DLQ isolation using fixture queues.

No real dispatch queue is consumed, purged or changed. Removes only the two
uniquely named queues this script creates. Run in the development API container.
"""

import json
import time
from uuid import uuid4

import boto3


def main():
    sqs = boto3.client(
        "sqs",
        endpoint_url="http://127.0.0.1:9324",
        region_name="us-east-1",
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )
    name = f"oci-proof-{uuid4().hex}"
    dlq = sqs.create_queue(QueueName=f"{name}-dlq")["QueueUrl"]
    work = None
    try:
        arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"][
            "QueueArn"
        ]
        work = sqs.create_queue(
            QueueName=name,
            Attributes={
                "VisibilityTimeout": "0",
                "RedrivePolicy": json.dumps({"deadLetterTargetArn": arn, "maxReceiveCount": 2}),
            },
        )["QueueUrl"]
        sqs.send_message(QueueUrl=work, MessageBody=json.dumps({"proof": name}))
        counts = []
        for _ in range(20):
            response = sqs.receive_message(
                QueueUrl=work, WaitTimeSeconds=0, AttributeNames=["ApproximateReceiveCount"]
            )
            for message in response.get("Messages", []):
                counts.append(int(message["Attributes"]["ApproximateReceiveCount"]))
                # Simulate failed admission: deliberately do not acknowledge.
            dead = sqs.receive_message(QueueUrl=dlq, WaitTimeSeconds=0)
            if dead.get("Messages"):
                assert json.loads(dead["Messages"][0]["Body"])["proof"] == name
                assert counts == [1, 2], counts
                print(
                    json.dumps(
                        {
                            "status": "passed",
                            "visibility_retry": True,
                            "max_receive_count": 2,
                            "dead_lettered": True,
                        }
                    )
                )
                return
            time.sleep(0.25)
        raise RuntimeError("Fixture message did not reach its DLQ")
    finally:
        if work:
            sqs.delete_queue(QueueUrl=work)
        sqs.delete_queue(QueueUrl=dlq)


if __name__ == "__main__":
    main()
