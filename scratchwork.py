import requests
import xml.etree.ElementTree as ET

headers = {
    'accept': '*/*',
    'x-api-key': EB_KEY,
}

response = requests.get('https://syndication.api.eb.com/production/article/39330/xml', headers=headers)

response_xml_as_string = response.text.encode('utf-8')
print(response_xml_as_string)
asXML = ET.fromstring(response_xml_as_string)

rv = ''
for p in asXML.findall(".//p"):
    # Get all inner text
    rv = rv + "".join(t.encode('utf-8') for t in p.itertext())

rv = rv.split(",",1)[1]

converter.say(rv)
converter.runAndWait()
