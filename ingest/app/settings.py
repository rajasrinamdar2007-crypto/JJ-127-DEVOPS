import os

from dotenv import load_dotenv

from app import contract

load_dotenv()

MQTT_HOST = os.getenv("MQTT_HOST", contract.TRANSPORT["broker_host"])
MQTT_PORT = int(os.getenv("MQTT_PORT", str(contract.TRANSPORT["port"])))
MQTT_TOPIC = os.getenv(
    "MQTT_TOPIC",
    contract.TRANSPORT["topic_pattern"].replace("{camera_id}", "+"),
)
MQTT_QOS = int(os.getenv("MQTT_QOS", str(contract.TRANSPORT["qos"])))
MQTT_KEEPALIVE = int(os.getenv("MQTT_KEEPALIVE", "60"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")

POSTGRES_HOST = os.getenv("POSTGRES_HOST", "db")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_USER = os.getenv("POSTGRES_USER", "anpruser")
# No default on purpose, for the same reason as gis/app/settings.py: a fallback would be a
# published credential that also lets the service authenticate when it should not.
POSTGRES_PASSWORD = os.environ["POSTGRES_PASSWORD"]
POSTGRES_DB = os.getenv("POSTGRES_DB", "anpr")
