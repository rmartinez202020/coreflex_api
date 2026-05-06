import os
import redis

REDIS_URL = os.getenv("REDIS_URL")

redis_client = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True,
)

def redis_test():
    redis_client.set("test", "coreflex")
    return redis_client.get("test")