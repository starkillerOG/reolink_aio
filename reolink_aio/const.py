"""Constants for Reolink"""

WAKING_COMMANDS = {
    "GetEnc",
    "GetWhiteLed",
    "GetZoomFocus",
    "GetAudioCfg",
    "GetPtzGuard",
    "GetAutoReply",
    "GetPtzTraceSection",
    "GetAiCfg",
    "GetAiAlarm",
    "GetPtzCurPos",
    "GetAudioAlarm",
    "GetDingDongList",
    "GetDingDongCfg",
    "DingDongOpt",
    "GetPerformance",
    "GetMask",
    "296",
    "483",
}

NONE_WAKING_COMMANDS = {
    "GetIsp",
    "GetEvents",
    "GetMdState",
    "GetAiState",
    "GetIrLights",
    "GetBatteryInfo",
    "GetPirInfo",
    "GetPowerLed",
    "GetAutoFocus",
    "GetDeviceAudioCfg",
    "GetManualRec",
    "GetImage",
    "GetBuzzerAlarmV20",
    "GetEmailV20",
    "GetEmail",
    "GetPushV20",
    "GetPush",
    "GetFtpV20",
    "GetFtp",
    "GetRecV20",
    "GetRec",
    "GetAudioAlarmV20",
    "GetMdAlarm",
    "GetAlarm",
    "GetStateLight",
    "GetHddInfo",
    "GetChannelstatus",
    "GetPushCfg",
    "GetScene",
    "115",
    "208",
    "594",
}

UNKNOWN = "Unknown"

MIN_COLOR_TEMP = 3000
MAX_COLOR_TEMP = 6000

AI_DETECT_CONVERSION = {"person": "people", "pet": "dog_cat"}

YOLO_CONVERSION = {"person": "people", "motor vehicle": "vehicle", "animal": "dog_cat"}
YOLO_DETECTS = {"people", "vehicle", "package", "non-motor vehicle", "dog_cat"}
YOLO_DETECT_TYPES = {
    "people": ["man", "woman"],
    "vehicle": ["sedan", "suv", "pickup_truck", "bus", "motorcycle"],
    "dog_cat": ["dog", "cat"],
    "package": ["package"],
    "non-motor vehicle": ["bicycle"],
}
