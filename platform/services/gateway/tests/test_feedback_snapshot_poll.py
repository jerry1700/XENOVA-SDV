"""기본 feedback와 feedback3가 poll의 최종 장치 view를 한 번만 게시하는지 확인한다."""
from collections import OrderedDict
from types import SimpleNamespace

from xenova_gateway.feedback import FeedbackSerialSink
from xenova_gateway.feedback3 import Feedback3SerialSink


def sink(kind):
    value=object.__new__(kind)
    value.started=0.;value.ready=False;value.timeout=.5;value.clock=lambda:0.1
    value.fault=None;value.hello_sent_us=100000
    value.pending=OrderedDict();value.odometry=None
    value.framer=SimpleNamespace(invalid=0,oversize=0,frames=lambda raw:())
    value._serial=SimpleNamespace(read=lambda size:b'')
    value.tof_inbox=SimpleNamespace(reject=lambda **kwargs:None)
    views=[]
    value._view=lambda:views.append('final') or {'ready':False}
    return value,views


def test_feedback3_does_not_publish_the_intermediate_base_view():
    value,views=sink(Feedback3SerialSink)
    value._poll(.1)
    assert views==['final']
    assert value.snapshot=={'ready':False}


def test_plain_feedback_retains_its_final_snapshot():
    value,views=sink(FeedbackSerialSink)
    value._poll(.1)
    assert views==['final']
    assert value.snapshot=={'ready':False}
