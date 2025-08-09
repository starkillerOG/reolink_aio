"""Reolink Baichuan XML templates."""

XML_HEADER = """<?xml version="1.0" encoding="UTF-8" ?>
"""

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

DingDongOpt_1_XML = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<dingdongDeviceOpt version="1.1">
<opt>delDevice</opt>
<id>{chime_id}</id>
</dingdongDeviceOpt>
</body>
"""

DingDongOpt_2_XML = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<dingdongDeviceOpt version="1.1">
<id>{chime_id}</id>
<opt>getParam</opt>
</dingdongDeviceOpt>
</body>
"""

DingDongOpt_3_XML = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<dingdongDeviceOpt version="1.1">
<opt>setParam</opt>
<id>{chime_id}</id>
<volLevel>{vol}</volLevel>
<ledState>{led}</ledState>
<name>{name}</name>
</dingdongDeviceOpt>
</body>
"""

DingDongOpt_4_XML = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<dingdongDeviceOpt version="1.1">
<id>{chime_id}</id>
<opt>ringWithMusic</opt>
<musicId>{tone_id}</musicId>
</dingdongDeviceOpt>
</body>
"""

SetDingDongCfg_XML = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<dingdongCfg version="1.1">
<deviceCfg>
<id>{chime_id}</id>
<alarminCfg>
<valid>{state}</valid>
<musicId>{tone_id}</musicId>
<type>{event_type}</type>
</alarminCfg>
</deviceCfg>
</dingdongCfg>
</body>
"""

GetDingDongCtrl_XML = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<dingdongCtrl version="1.1">
<opt>machineStateGet</opt>
</dingdongCtrl>
</body>
"""

SetDingDongCtrl_XML = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<dingdongCtrl version="1.1">
<opt>machineStateSet</opt>
<type>{chime_type}</type>
<bopen>{enabled}</bopen>
<bsave>1</bsave>
<time>{time}</time>
</dingdongCtrl>
</body>
"""

QuickReplyPlay_XML = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<audioFileInfo version="1.1">
<channelId>{channel}</channelId>
<id>{file_id}</id>
<timeout>0</timeout>
</audioFileInfo>
</body>
"""

SetRecEnable = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<Record version="1.1">
<channelId>{channel}</channelId>
<enable>{enable}</enable>
</Record>
</body>
"""

SetPrivacyMode = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<sleepState version="1.1">
<operate>2</operate>
<sleep>{enable}</sleep>
</sleepState>
</body>"""

GetSceneInfo = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<sceneCfg version="1.1">
<id>{scene_id}</id>
</sceneCfg>
</body>"""

DisableScene = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<sceneModeCfg version="1.1">
<enable>0</enable>
</sceneModeCfg>
</body>"""

SetScene = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<sceneModeCfg version="1.1">
<enable>1</enable>
<curSceneId>{scene_id}</curSceneId>
</sceneModeCfg>
</body>"""

FileInfoListOpen = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<FileInfoList version="1.1">
<FileInfo>
<uid>{uid}</uid>
<searchAITrack>1</searchAITrack>
<channelId>{channel}</channelId>
<logicChnBitmap>255</logicChnBitmap>
<streamType>mainStream</streamType>
<recordType>manual, sched, io, md, people, face, vehicle, dog_cat, visitor, other, package</recordType>
<startTime>
<year>{start_year}</year>
<month>{start_month}</month>
<day>{start_day}</day>
<hour>{start_hour}</hour>
<minute>{start_minute}</minute>
<second>{start_second}</second>
</startTime>
<endTime>
<year>{end_year}</year>
<month>{end_month}</month>
<day>{end_day}</day>
<hour>{end_hour}</hour>
<minute>{end_minute}</minute>
<second>{end_second}</second>
</endTime>
</FileInfo>
</FileInfoList>
</body>"""

FileInfoList = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<FileInfoList version="1.1">
<FileInfo>
<channelId>{channel}</channelId>
<uid>{uid}</uid>
<searchAITrack>1</searchAITrack>
<handle>{handle}</handle>
</FileInfo>
</FileInfoList>
</body>"""

FindRecVideoOpen = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<findAlarmVideo version="1.1">
<channelId>{channel}</channelId>
<uid>{uid}</uid>
<logicChnBitmap>255</logicChnBitmap>
<streamType>{stream_type}</streamType>
<notSearchVideo>0</notSearchVideo>
<startTime>
<year>{start_year}</year>
<month>{start_month}</month>
<day>{start_day}</day>
<hour>{start_hour}</hour>
<minute>{start_minute}</minute>
<second>{start_second}</second>
</startTime>
<endTime>
<year>{end_year}</year>
<month>{end_month}</month>
<day>{end_day}</day>
<hour>{end_hour}</hour>
<minute>{end_minute}</minute>
<second>{end_second}</second>
</endTime>
<alarmType>md, pir, io, people, face, vehicle, dog_cat, visitor, other, package, cry, crossline, intrusion, loitering, legacy, loss</alarmType>
</findAlarmVideo>
</body>"""

FindRecVideo = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<findAlarmVideo version="1.1">
<channelId>{channel}</channelId>
<fileHandle>{fileHandle}</fileHandle>
</findAlarmVideo>
</body>"""

UserList = """
<?xml version="1.0" encoding="UTF-8" ?>
<Extension version="1.1">
<userName>{username}</userName>
</Extension>"""

SetWhiteLed = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<FloodlightManual version="1.1">
<channelId>{channel}</channelId>
<status>{state}</status>
<duration>180</duration>
</FloodlightManual>
</body>"""

WifiSSID = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<Wifi version="1.1">
<scanAp>0</scanAp>
</Wifi>
</body>"""

PreRecord = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<longRunModeCfg version="1.1">
<enable>{enable}</enable>
<value>{batteryStop}</value>
<preTime>{preTime}</preTime>
<usePlanList>{schedule}</usePlanList>
<fps>{fps}</fps>
</longRunModeCfg>
</body>"""

SirenManual = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<audioPlayInfo version="1.1">
<channelId>{channel}</channelId>
<playMode>2</playMode>
<playDuration>10</playDuration>
<playTimes>1</playTimes>
<onOff>{enable}</onOff>
</audioPlayInfo>
</body>"""

SirenTimes = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<audioPlayInfo version="1.1">
<channelId>{channel}</channelId>
<playMode>0</playMode>
<playDuration>10</playDuration>
<playTimes>{times}</playTimes>
<onOff>1</onOff>
</audioPlayInfo>
</body>"""

SirenHubManual = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<audioPlayInfo version="1.1">
<playMode>2</playMode>
<playDuration>10</playDuration>
<playTimes>1</playTimes>
<onOff>{enable}</onOff>
</audioPlayInfo>
</body>"""

SirenHubTimes = """
<?xml version="1.0" encoding="UTF-8" ?>
<body>
<audioPlayInfo version="1.1">
<playMode>0</playMode>
<playDuration>10</playDuration>
<playTimes>{times}</playTimes>
<onOff>1</onOff>
</audioPlayInfo>
</body>"""
