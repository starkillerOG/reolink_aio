"""Reolink Baichuan XML templates."""

XML_HEADER = """<?xml version="1.0" encoding="UTF-8" ?>"""

LOGIN_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<body>
<LoginUser version="1.1">
<userName>{userName}</userName>
<password>{password}</password>
<userVer>1</userVer>
</LoginUser>
<LoginNet version="1.1">
<type>LAN</type>
<udpPort>0</udpPort>
</LoginNet>
</body>
"""

LOGOUT_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<body>
<LoginUser version="1.1">
<userName>{userName}</userName>
<password>{password}</password>
<userVer>1</userVer>
</LoginUser>
</body>
"""

CHANNEL_EXTENSION_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<Extension version="1.1">
<channelId>{channel}</channelId>
</Extension>
"""
