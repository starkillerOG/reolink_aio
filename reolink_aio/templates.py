"""Reolink NVR/camera API."""

HEADERS = {"Content-Type": "application/soap+xml;charset=UTF-8"}


SUBSCRIBE_ACTION = {"action": "http://docs.oasis-open.org/wsn/bw-2/NotificationProducer/SubscribeRequest"}
RENEW_ACTION = {"action": "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest"}
UNSUBSCRIBE_ACTION = {"action": "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest"}


SUBSCRIBE_XML = """
    <soap:Envelope xmlns:add="http://www.w3.org/2005/08/addressing" xmlns:b="http://docs.oasis-open.org/wsn/b-2" xmlns:soap="http://www.w3.org/2003/05/soap-envelope">
        <soap:Header>
            <add:Action>http://docs.oasis-open.org/wsn/bw-2/NotificationProducer/SubscribeRequest</add:Action>
            <wsse:Security soap:mustUnderstand="true" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
                xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
                <wsse:UsernameToken wsu:Id="UsernameToken-{UsernameToken}">
                    <wsse:Username>{Username}</wsse:Username>
                    <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{PasswordDigest}</wsse:Password>
                    <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{Nonce}</wsse:Nonce>
                    <wsu:Created>{Created}</wsu:Created>
                </wsse:UsernameToken>
            </wsse:Security>
        </soap:Header>
        <soap:Body>
            <b:Subscribe>
                <b:ConsumerReference>
                    <add:Address>{Address}</add:Address>
                </b:ConsumerReference>
                <b:InitialTerminationTime>{InitialTerminationTime}</b:InitialTerminationTime>
            </b:Subscribe>
        </soap:Body>
    </soap:Envelope>
"""


RENEW_XML = """
    <soap:Envelope xmlns:add="http://www.w3.org/2005/08/addressing" xmlns:b="http://docs.oasis-open.org/wsn/b-2" xmlns:soap="http://www.w3.org/2003/05/soap-envelope">
        <soap:Header>
            <add:Action>http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest</add:Action>
            <add:To>{To}</add:To>
            <wsse:Security soap:mustUnderstand="true" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
                xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
                <wsse:UsernameToken wsu:Id="UsernameToken-{UsernameToken}">
                    <wsse:Username>{Username}</wsse:Username>
                    <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{PasswordDigest}</wsse:Password>
                    <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{Nonce}</wsse:Nonce>
                    <wsu:Created>{Created}</wsu:Created>
                </wsse:UsernameToken>
            </wsse:Security>
        </soap:Header>
        <soap:Body>
            <b:Renew>
                <b:TerminationTime>{TerminationTime}</b:TerminationTime>
            </b:Renew>
        </soap:Body>
    </soap:Envelope>
"""


UNSUBSCRIBE_XML = """
    <soap:Envelope xmlns:add="http://www.w3.org/2005/08/addressing" xmlns:b="http://docs.oasis-open.org/wsn/b-2" xmlns:soap="http://www.w3.org/2003/05/soap-envelope">
        <soap:Header>
            <add:Action>http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest</add:Action>
            <add:To>{To}</add:To>
            <wsse:Security soap:mustUnderstand="true" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
                xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
                <wsse:UsernameToken wsu:Id="UsernameToken-{UsernameToken}">
                    <wsse:Username>{Username}</wsse:Username>
                    <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{PasswordDigest}</wsse:Password>
                    <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{Nonce}</wsse:Nonce>
                    <wsu:Created>{Created}</wsu:Created>
                </wsse:UsernameToken>
            </wsse:Security>
        </soap:Header>
        <soap:Body>
            <b:Unsubscribe/>
        </soap:Body>
    </soap:Envelope>
"""
