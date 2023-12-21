import logging
from os.path import basename
from typing import TypeAlias, Dict

from xml.etree import ElementTree as XML

_LOGGER = logging.getLogger(__name__)

"""
Channel is either the channel selected by the user, or the channel reported by the message.
If not user sleected, the channel should be an int (in the normal case), or a string (the message
specified a channel that coudldn't be parsed) or None (the message specified no channel)
"""
Channel: TypeAlias = int|str|None

"""
EventName is the event name reported by the message. These should be FaceDetect, Motion, 
MotionAlarm, etc, however no validation is performed on the event names and these will
contain whatever was sent by the camera.
"""
EventName: TypeAlias = bool|None

"""
State: True (active), False (inactive) or None (failed to parse message)
"""
EventState: TypeAlias = bool|None

"""
A map of all events parsed in a message (valid or invalid)
"""
EventsMap: TypeAlias = Dict[Channel, Dict[EventName, EventState]]


def parse_reolink_onvif_event(
        data: str,
        root: XML.Element | None = None,
        user_selected_channel: int | None = None
    ) -> EventsMap:
    """ Parses a webhook message received from a Reolink event subscription. If user_selected_channel
    is specified, all events will assume to be for that channel, and no channel checking will be
    performed. """

    parsed_events: EventsMap = {}

    if root is None:
        root = XML.fromstring(data)

    for message in root.iter("{http://docs.oasis-open.org/wsn/b-2}NotificationMessage"):
        channel = user_selected_channel

        # find NotificationMessage Rule (type of event)
        topic_element = message.find("{http://docs.oasis-open.org/wsn/b-2}Topic[@Dialect='http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet']")
        if topic_element is None or topic_element.text is None:
            continue
        rule = basename(topic_element.text)
        if not rule:
            continue

        # find camera channel if not specified by caller
        if channel is None:
            source_element = message.find(".//{http://www.onvif.org/ver10/schema}SimpleItem[@Name='Source']")
            if source_element is None:
                source_element = message.find(".//{http://www.onvif.org/ver10/schema}SimpleItem[@Name='VideoSourceConfigurationToken']")
            if source_element is not None and "Value" in source_element.attrib:
                try:
                    channel = int(source_element.attrib["Value"])
                except ValueError:
                    _LOGGER.warning("Reolink ONVIF event '%s' data contained invalid channel '%s'", rule, source_element.attrib["Value"])
                    channel = str(source_element.attrib["Value"])
            else:
                _LOGGER.warning("Reolink ONVIF event '%s' does not contain channel", rule)

        # find event state
        key = "State"
        if rule == "Motion":
            key = "IsMotion"
        data_element = message.find(f".//\u007bhttp://www.onvif.org/ver10/schema\u007dSimpleItem[@Name='{key}']")
        if data_element is None or "Value" not in data_element.attrib:
            _LOGGER.warning("ONVIF event '%s' did not contain data:\n%s", rule, data)
            state = None
        else:
            state = data_element.attrib["Value"] == "true"

        _LOGGER.info("Reolink %s ONVIF event channel %s, %s: %s", channel, rule, state)
        if channel not in parsed_events:
            parsed_events[channel] = {}
        parsed_events[channel][rule] = state

    return parsed_events

