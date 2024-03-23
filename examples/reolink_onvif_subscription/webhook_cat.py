from flask import Flask
from flask import request as FlaskRequest

import sys
sys.path.append('../../')

from reolink_aio.helpers import parse_reolink_onvif_event

import logging
logging.getLogger("reolink_aio.helpers").setLevel(logging.ERROR)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

@app.route('/', defaults={'path': ''}, methods=['GET', 'POST'])
@app.route('/<path:path>', methods=['GET', 'POST'])
def catch_all(path):
    print(parse_reolink_onvif_event(FlaskRequest.data))
    return '', 200

app.run(port=1234, host='0.0.0.0', debug=True)
