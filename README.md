<h2 align="center">
  <a href="https://reolink.com"><img src="https://raw.githubusercontent.com/starkillerOG/reolink_aio/master/doc/logo.png" width="200"></a>
  <br>
  <i>Reolink NVR/cameras API package</i>
  <br>
</h2>

<p align="center">
  <a href="https://github.com/sponsors/starkillerOG"><img src="https://img.shields.io/static/v1?label=Sponsor&message=%E2%9D%A4&logo=GitHub&color=%23fe8e86" alt="Sponsor"></a>
  <a href="https://reolink.pxf.io/q44QWq"><img src="https://img.shields.io/static/v1?label=Affiliate link&message=%E2%9D%A4&color=%23fe8e86" alt="Affiliate link"></a>
  <a href="https://pypi.org/project/reolink-aio"><img src="https://img.shields.io/pypi/dm/reolink-aio"></a>
  <a href="https://github.com/starkillerOG/reolink_aio/releases"><img src="https://img.shields.io/github/v/release/StarkillerOG/reolink_aio?display_name=tag&include_prereleases&sort=semver" alt="Current version"></a>
</p>

The `reolink_aio` Python package allows you to integrate your [Reolink](https://www.reolink.com/) devices (NVR/cameras) in your application.

### Description

This is a package implementing Reolink IP NVR and camera API. Also it’s providing a way to subscribe to Reolink ONVIF SWN events, so that real-time events can be received on a webhook.

### Show your appreciation

If you appreciate the reolink integration and want to support its development, please consider [sponsering](https://github.com/sponsors/starkillerOG) the upstream library or purchase Reolink products through [this affiliate link](https://reolink.pxf.io/q44QWq).

### Prerequisites

- Python 3.11

### Installation

```
pip3 install reolink-aio
```

or manually:
````bash
git clone https://github.com/StarkillerOG/reolink_aio
cd reolink_aio/
pip3 install .
````

### Usage

````python
from reolink_aio.api import Host
import asyncio

# Create a host-object (representing either a camera, or NVR with several channels)
host = Host('192.168.1.10', 'user', 'mypassword')

# Obtain/cache NVR or camera settings and capabilities, like model name, ports, HDD size, etc:
await host.get_host_data()

# Get the subscribtion port and host-device name:
subscribtion_port =  host.onvif_port
name = host.nvr_name

# Obtain/cache states of features:
await host.get_states()

# Print some state value on the channel with index 0:
print(host.ir_enabled(0))

# Enable the infrared lights on the channel with index 1:
await host.set_ir_lights(1, True)

# Enable the spotlight on the channel with index 1:
await host.set_spotlight(1, True)

# Enable the siren on the channel with index 0:
await host.set_siren(0, True)

# Now subscribe to events, suppose our webhook url is http://192.168.1.11/webhook123
await host.subscribe('http://192.168.1.11/webhook123')

# After some minutes check the renew timer (keep the eventing alive):
if (host.renewtimer() <= 100):
    await host.renew()

# Logout and disconnect
await host.logout()
````

### Example

This is an example of the usage of the API. In this case we want to retrive and print the Mac Address of the NVR.
````python
from reolink_aio.api import Host
import asyncio

async def print_mac_address():
    # initialize the host
    host = Host('192.168.1.109','admin', 'admin1234', port=80)
    # connect and obtain/cache device settings and capabilities
    await host.get_host_data()
    # check if it is a camera or an NVR
    print(f"It is an NVR: {host.is_nvr}, number of channels: {host.num_channel}")
    # print mac address
    print(f"MAC: {host.mac_address}")
    # close the device connection
    await host.logout()

if __name__ == "__main__":
    asyncio.run(print_mac_address())
````

### TCP push event example
This is an example of how to receive TCP push events.
The callback will be called each time a push is received.
The state variables of the Host class will automatically be updated when a push comes in.
````python
from reolink_aio.api import Host
import asyncio
import logging

logging.basicConfig(level="INFO")
_LOGGER = logging.getLogger(__name__)

def callback() -> None:
    _LOGGER.info("Callback called")

async def tcp_push_demo():
    # initialize the host
    host = Host(host="192.168.1.109", username="admin", password="admin1234")
    # connect and obtain/cache device settings and capabilities
    await host.get_host_data()
    # Register callback and subscribe to events
    host.baichuan.register_callback("unique_id_string", callback)
    await host.baichuan.subscribe_events()
    # Process TCP events for 2 minutes
    await asyncio.sleep(120)
    # unsubscribe and close the device connection
    await host.baichuan.unsubscribe_events()
    await host.logout()

if __name__ == "__main__":
    asyncio.run(tcp_push_demo())
````

### Acknowledgment
This library is based on the work of:
- @fwestenberg: https://github.com/fwestenberg/reolink_dev

The baichuan part of this library is based on the work of:
- @QuantumEntangledAndy and @thirtythreeforty: https://github.com/QuantumEntangledAndy/neolink

**Author**

@starkillerOG: https://github.com/starkillerOG

**Contributors**

- @xannor: https://github.com/xannor
- @mnpg: https://github.com/mnpg
