"""PC 입력 선기동 대기는 표본·출력 권한을 만들지 않고 현재 plant만 넘긴다."""
from types import SimpleNamespace
from unittest import TestCase
from tools.hil_observation.pc import _capture_attachment


class CaptureStartTests(TestCase):
    def test_feedback_can_move_before_first_capture_and_attach_is_once(self):
        now=[10.];seen=[];states=[]
        producer=SimpleNamespace(attach=lambda plant,peer:seen.append((now[0],plant.pose,peer)))
        feed=SimpleNamespace(attach=lambda **kw:None,offer_state=lambda *row:states.append(row))
        plant=SimpleNamespace(pose=0);peer=object()
        attach,advance=_capture_attachment(producer,feed,3.,clock=lambda:now[0])
        attach(plant,peer)
        self.assertEqual(seen,[])
        now[0]=12.9;plant.pose=2;advance()
        self.assertEqual(seen,[])
        now[0]=13.;plant.pose=3;advance();advance()
        self.assertEqual(seen,[(13.,3,peer)])
        self.assertEqual(states,[('WAITING_CAPTURE','WAITING'),('CAPTURING','RUNNING')])
        with self.assertRaises(ValueError):attach(plant,peer)

    def test_default_has_no_delay_or_second_owner(self):
        seen=[];plant=object();peer=object()
        producer=SimpleNamespace(attach=lambda *args:seen.append(args))
        attach,advance=_capture_attachment(producer,None,0,clock=lambda:10.)
        attach(plant,peer);advance()
        self.assertEqual(seen,[(plant,peer)])

    def test_invalid_wait_cannot_start_capture(self):
        for value in (-1,float('nan'),float('inf'),True):
            with self.subTest(value=value),self.assertRaises(ValueError):
                _capture_attachment(None,None,value)
