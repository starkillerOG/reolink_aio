import signal
import time
import asyncio
import sys

sys.path.append('../../')

from reolink_aio.api import Host

import logging
logging.getLogger("reolink_aio.helpers").setLevel(logging.ERROR)

if len(sys.argv) != 5:
    print(f"Usage: {sys.argv[0]} CAM_HOST CAM_USER CAM_PASS WEBHOOK_URL")
    print("\tCAM_HOST: The IP of your camera in your LAN")
    print("\tCAM_USER: User name you use to login in the web interface")
    print("\tCAM_PASS: Pass for CAM_USER. Use '' for blank password")
    print("\tWEBHOOK_URL: A URL reachable by the camera. In this example, it should be the")
    print("\t  list URI of webhook_cat.py")
    print("\t  EG: 'http://192.168.1.20:1234/webhook'")
    exit(1)

CAM_HOST=sys.argv[1]
CAM_USER=sys.argv[2]
CAM_PASS=sys.argv[3]
WEBHOOK_URL=sys.argv[4]


gUserExitRq = False
def on_signal(*args):
    global gUserExitRq
    print("User requested exit, cleanup...")
    gUserExitRq = True
signal.signal(signal.SIGINT, on_signal)
signal.signal(signal.SIGTERM, on_signal)


async def main():
    host = Host(CAM_HOST, CAM_USER, CAM_PASS, use_https=True)

    # Obtain/cache NVR or camera settings and capabilities, like model name, ports, HDD size, etc:
    await host.get_host_data()

    # Obtain/cache states of features:
    await host.get_states()

    print("Connected to camera", host.camera_name(0))

    await host.subscribe(WEBHOOK_URL)
    print(f"Subscribed to {WEBHOOK_URL}")

    global gUserExitRq
    while not gUserExitRq:
        t = host.renewtimer()
        print("Subscription seconds remaining: ", t)
        if (t <= 100):
            await host.renew()
        time.sleep(1)

    await host.unsubscribe()
    await host.logout()

asyncio.run(main())
