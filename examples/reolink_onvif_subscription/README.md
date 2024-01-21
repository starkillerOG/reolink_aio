# Reolink ONVIF subscription example

This example shows how to point a Reolink camera to a webhook endpoint to receive ONVIF events, and how to parse those events.

# Dependencies

To run these examples you'll need the following dependencies:

```
python3 -m pipenv install flask aiohttp orjson typing_extensions
```

# Running

First start the webhook_cat.py service:

```
python3 -m pipenv run python ./webhook_cat.py
```

This will start a Flask service, which will listen to all incoming messages in port 1234 in all available interfaces. When a message is received, it will try to parse it as a Reolink ONVIF message (or fail misserably).

Take note of the interface and port in which webhook_cat is listening, and make sure your camera can access this URI.

Next, configure the camera to use the webhook_cat service:

```
python3 -m pipenv run python ./trigger_subscription.py CAM_HOST CAM_USER CAM_PASS WEBHOOK_CAT_URI
```

If everything worked, webhook_cat should now be printing notifications from your camera.

