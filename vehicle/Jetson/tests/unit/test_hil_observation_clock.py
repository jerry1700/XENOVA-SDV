"""개발 PC↔J 4시각의 유한 수학/원문 회귀다. 실제 네트워크나 clock 실측 시험은 아니다.

아래 tick과 상대 rate는 독립 software 입력이다. map 결과는 원 PC pair와 따로
검사하며 READY/관측 freshness/Pi P4/World authority를 이 시험에서 발급하지 않는다.
"""

from dataclasses import FrozenInstanceError, replace
from fractions import Fraction
import json
import unittest
from uuid import UUID

from tools.hil_observation.clock import (ClockSource, ClockPolicy, ClockProbe, ClockReply,
    ClockLease, HilObservationClock, HilClockError, reply, U63_MAX)


def source(): return ClockSource(*(str(UUID(int=n)) for n in (1, 2, 3)))


def policy(**changes):
    fields=dict(max_rtt_us=100, max_offset_width_us=32, drift_ppm=0, lease_us=1000, lookback_us=500)
    fields.update(changes)
    return ClockPolicy(**fields)


def sample(owner, sequence, j1, *, p2=None, p3=None, j4=None):
    probe=owner.probe(sequence, sequence.to_bytes(16,'big'), j1, j1+200)
    value=reply(probe,j1-996 if p2 is None else p2,j1-995 if p3 is None else p3)
    return value,owner.accept(value,j1+10 if j4 is None else j4)


def warm(**changes):
    owner=HilObservationClock(source(),policy(**changes))
    first,none=sample(owner,1,10_000)
    assert none is None
    second,lease=sample(owner,2,10_100)
    assert lease is not None
    return owner,first,second,lease


class HilObservationClockTests(unittest.TestCase):
    def test_original_four_times_need_two_samples_and_map_contains_true_pair_without_mutation(self):
        owner=HilObservationClock(source(),policy())
        first,none=sample(owner,1,10_000)
        self.assertIsNone(none);self.assertIsNone(owner.lease)
        second,lease=sample(owner,2,10_100)
        self.assertIs(lease,owner.lease)
        self.assertEqual((lease.j1_us,lease.p2_us,lease.p3_us,lease.j4_us),(10_100,9104,9105,10_110))
        self.assertEqual((lease.offset_lower_us,lease.offset_upper_us),(995,1006))
        self.assertEqual((lease.support_lower_j_us,lease.expires_j_us),(9600,11_100))
        original=(9150,9153)
        mapped=lease.map_interval(*original,now_j=10_400)
        self.assertEqual(original,(9150,9153));self.assertEqual(mapped,(10_145,10_159))
        self.assertLessEqual(mapped[0],10_150);self.assertGreaterEqual(mapped[1],10_153)
        self.assertEqual(lease.raw_probe_bytes,second.probe.raw_bytes)
        self.assertEqual(lease.raw_reply_bytes,second.raw_bytes)
        self.assertNotEqual(lease.raw_reply_bytes,first.raw_bytes)
        with self.assertRaises(FrozenInstanceError):lease.j4_us=10_400
        with self.assertRaises(TypeError):ClockLease()

    def test_exact_canonical_control_and_quantization_bucket_cover_same_tick_events(self):
        owner=HilObservationClock(source(),policy())
        sample(owner,1,10_000,p2=9000,p3=9000,j4=10_000)
        value,lease=sample(owner,2,10_100,p2=9100,p3=9100,j4=10_100)
        self.assertEqual((lease.offset_lower_us,lease.offset_upper_us),(999,1001))
        self.assertEqual(lease.map_interval(9200,9201,10_400),(10_199,10_202))
        self.assertEqual(ClockReply.from_raw(value.raw_bytes),value)
        self.assertEqual(ClockProbe.from_raw(value.probe.raw_bytes),value.probe)
        fields=value.fields;fields['probe']['source']['source_id']=str(UUID(int=9))
        self.assertEqual(value.probe.source,source())
        for raw in (b' '+value.raw_bytes,json.dumps(value.fields,indent=2).encode('ascii'),
                value.raw_bytes.replace(b'"p2_us":"9100"',b'"p2_us":"09100"')):
            with self.subTest(raw=raw[:30]),self.assertRaises(HilClockError):ClockReply.from_raw(raw)
        bad=value.fields;bad['unexpected']=0
        with self.assertRaises(HilClockError):ClockReply.from_fields(bad)
        bad=value.probe.fields;bad['nonce']='A'*32
        with self.assertRaises(HilClockError):ClockProbe.from_fields(bad)

    def test_duplicate_keeps_original_j4_and_expiry_and_conflicting_original_body_revokes(self):
        owner,first,second,lease=warm()
        self.assertIs(owner.probe(2,second.probe.nonce,10_100,10_300),second.probe)
        self.assertIs(owner.accept(second,20_000),lease)
        self.assertIsNone(owner.accept(first,20_001))
        self.assertEqual((lease.j4_us,lease.expires_j_us),(10_110,11_100))
        with self.assertRaises(HilClockError):lease.map_interval(9150,9153,20_000)
        with self.assertRaisesRegex(HilClockError,'CLOCK_REPLY_CONFLICT'):
            owner.accept(replace(second,p3_us=second.p3_us+1),10_112)
        self.assertIsNone(owner.lease);self.assertEqual(owner.sample_count,0)
        self.assertFalse(owner.replace_source(source()))
        with self.assertRaisesRegex(HilClockError,'CLOCK_REVOKED'):
            owner.probe(3,(3).to_bytes(16,'big'),10_200,10_400)
        other=HilObservationClock(source(),policy());sample(other,1,10_000)
        with self.assertRaisesRegex(HilClockError,'CLOCK_NONCE_CONFLICT'):
            other.probe(2,(1).to_bytes(16,'big'),10_100,10_300)
        self.assertIsNone(other.lease)
        other=HilObservationClock(source(),policy());sample(other,1,10_000)
        with self.assertRaisesRegex(HilClockError,'CLOCK_PROBE_CONFLICT'):
            other.probe(1,b'x'*16,10_000,10_200)

    def test_failed_new_warmup_preserves_old_lease_and_requires_two_new_valid_samples(self):
        owner,_,_,original=warm()
        pending=owner.probe(3,(3).to_bytes(16,'big'),10_200,10_400)
        with self.assertRaisesRegex(HilClockError,'CLOCK_RTT'):
            owner.accept(reply(pending,9204,9205),10_320)
        self.assertIs(owner.lease,original)
        _,none=sample(owner,4,10_350)
        self.assertIsNone(none);self.assertIs(owner.lease,original)
        _,fresh=sample(owner,5,10_450)
        self.assertIsNotNone(fresh);self.assertIs(owner.lease,fresh)
        self.assertEqual(original.expires_j_us,11_100)
        self.assertEqual(fresh.expires_j_us,11_450)
        self.assertLessEqual(owner.sample_count,2)
        # 같은 유효 window에서 불가능한 offset은 source clock 모순이므로 명시 철회한다.
        pending=owner.probe(6,(6).to_bytes(16,'big'),10_550,10_750)
        with self.assertRaisesRegex(HilClockError,'CLOCK_OFFSET_CONFLICT'):
            owner.accept(reply(pending,10_004,10_005),10_560)
        self.assertIsNone(owner.lease)
        self.assertEqual(fresh.expires_j_us,11_450)

    def test_foreign_peer_session_rejected_and_explicit_source_replacement_clears_current_port(self):
        owner,_,_,lease=warm()
        p=owner.probe(3,(3).to_bytes(16,'big'),10_200,10_400)
        foreign=replace(p,source=replace(source(),pc_session=str(UUID(int=4))))
        with self.assertRaisesRegex(HilClockError,'CLOCK_FOREIGN_SOURCE'):
            owner.accept(reply(foreign,9204,9205),10_210)
        self.assertIs(owner.lease,lease)
        new=replace(source(),jetson_session=str(UUID(int=5)))
        self.assertTrue(owner.replace_source(new));self.assertIsNone(owner.lease)
        self.assertEqual(owner.sample_count,0)
        with self.assertRaisesRegex(HilClockError,'CLOCK_FOREIGN_SOURCE'):
            owner.accept(reply(p,9204,9205),10_210)
        # 보관된 옛 lease는 역사 불변값이다. caller가 현재 source와 반드시 따로 대조한다.
        self.assertNotEqual(lease.source,owner.source)
        self.assertEqual((lease.j4_us,lease.expires_j_us),(10_110,11_100))

    def test_drift_is_outward_over_entire_original_window_for_both_clock_rates(self):
        for sign in (-1,1):
            with self.subTest(sign=sign):
                owner=HilObservationClock(source(),policy(drift_ppm=1000,max_offset_width_us=100))
                def pc(j):return Fraction(9000)+Fraction(j-10_000)*Fraction(1_000_000+sign*1000,1_000_000)
                def floor(value):return value.numerator//value.denominator
                def ceil(value):return -((-value.numerator)//value.denominator)
                for sequence,j1 in ((1,10_000),(2,10_100)):
                    _,lease=sample(owner,sequence,j1,p2=floor(pc(j1+4)),p3=floor(pc(j1+5)))
                raw=(floor(pc(10_400)),ceil(pc(10_405)))
                mapped=lease.map_interval(*raw,now_j=10_600)
                self.assertLessEqual(mapped[0],10_400);self.assertGreaterEqual(mapped[1],10_405)
                self.assertEqual(lease.drift_ppm,1000)
                zero_lower=lease.j1_us-lease.p2_us-1;zero_upper=lease.j4_us+1-lease.p3_us
                self.assertLess(lease.offset_lower_us,zero_lower)
                self.assertGreater(lease.offset_upper_us,zero_upper)

    def test_policy_original_order_rtt_width_and_native_integer_fail_closed(self):
        for key,value in (('max_rtt_us',True),('max_rtt_us',200_001),('max_offset_width_us',0),
                ('drift_ppm',1_000_000),('lease_us',1_000_001),('lookback_us',500_001)):
            with self.subTest(key=key),self.assertRaises(HilClockError):policy(**{key:value})
        with self.assertRaises(HilClockError):ClockSource('sim-wall:'+source().source_id,source().pc_session,source().jetson_session)
        for start,end in ((True,200),(10,10),(10,200_011),(U63_MAX-1,U63_MAX+1)):
            with self.subTest(start=start),self.assertRaises(HilClockError):ClockProbe(source(),1,b'1'*16,start,end)
        p=ClockProbe(source(),1,b'1'*16,10_000,10_200)
        with self.assertRaises(HilClockError):reply(p,9005,9004)
        with self.assertRaises(HilClockError):reply(p,True,9005)
        for j4,reason in ((9999,'J_ORDER'),(10_199,'EXPIRED')):
            owner=HilObservationClock(source(),policy());p=owner.probe(1,b'1'*16,10_000,10_200)
            with self.subTest(j4=j4),self.assertRaisesRegex(HilClockError,reason):owner.accept(reply(p,9004,9005),j4)
        owner=HilObservationClock(source(),policy(max_offset_width_us=2))
        with self.assertRaisesRegex(HilClockError,'OFFSET_WIDTH'):sample(owner,1,10_000)
        self.assertIsNone(owner.lease)
        owner,_,_,_=warm();pending=owner.probe(3,b'3'*16,10_200,10_400)
        with self.assertRaisesRegex(HilClockError,'CLOCK_PC_REGRESSION'):
            owner.accept(reply(pending,9000,9001),10_210)
        self.assertIsNone(owner.lease)

    def test_mapped_future_fixed_lookback_negative_u63_and_expiry_edges_are_rejected(self):
        _,_,_,lease=warm()
        for lo,hi,now in ((8604,8605,10_400),(9500,9501,10_400),(9150,9153,11_099),
                (9150,9153,11_100),(True,9153,10_400),(9153,9150,10_400),(U63_MAX,U63_MAX,10_400)):
            with self.subTest(lo=lo,now=now),self.assertRaises(HilClockError):lease.map_interval(lo,hi,now)
        self.assertEqual(lease.map_interval(9150,9153,11_098),(10_145,10_159))
        owner=HilObservationClock(source(),policy())
        sample(owner,1,10_000,p2=20_004,p3=20_005)
        _,negative=sample(owner,2,10_100,p2=20_104,p3=20_105)
        with self.assertRaises(HilClockError):negative.map_interval(0,1,10_400)
        # raw RTT10us의 원 tick hull은11us다. 명시 maxRTT10us를 조용히 늘리지 않는다.
        owner=HilObservationClock(source(),policy(max_rtt_us=10))
        with self.assertRaisesRegex(HilClockError,'CLOCK_RTT'):sample(owner,1,10_000)

    def test_deadline_lower_clips_future_pc_expiry_without_weakening_capture_mapping(self):
        _,_,_,lease=warm()
        self.assertEqual(lease.deadline_lower(20_000,10_400),lease.expires_j_us)
        self.assertEqual(lease.deadline_lower(U63_MAX,10_400),lease.expires_j_us)
        # future PC E는 관측이 아니다. 원 관측 map의 future/window guard는 그대로 거절한다.
        with self.assertRaises(HilClockError):lease.map_interval(20_000,20_000,10_400)
        deadline=lease.deadline_lower(9500,10_400)
        self.assertEqual(deadline,10_495)
        capture=lease.map_interval(9150,9153,10_400)
        observation_end=min(deadline,capture[0]+500_000)
        self.assertLess(capture[1],observation_end)
        # 선언된 offset 양 끝에서도 J<E이면 PC=J-offset<원PC E다.
        for offset in (995,1000,1006):self.assertLess(observation_end-1-offset,9500)
        self.assertEqual(lease.deadline_lower(9500,10_493),10_495)
        for pcE,nowJ in ((9500,10_494),(9500,10_600),(True,10_400),(U63_MAX+1,10_400),
                (0,10_400),(20_000,11_099),(20_000,11_100)):
            with self.subTest(pcE=pcE,nowJ=nowJ),self.assertRaises(HilClockError):
                lease.deadline_lower(pcE,nowJ)
        owner=HilObservationClock(source(),policy())
        sample(owner,1,10_000,p2=20_004,p3=20_005)
        _,negative=sample(owner,2,10_100,p2=20_104,p3=20_105)
        with self.assertRaises(HilClockError):negative.deadline_lower(0,10_400)
        self.assertEqual((lease.j4_us,lease.expires_j_us),(10_110,11_100))

    def test_lease_evidence_preserves_exact_issuance_pair_through_failed_warmup_and_fences(self):
        from tools.hil_observation.clock import ClockLeaseEvidence,ClockSampleEvidence
        owner,first,second,lease=warm()
        original=owner.lease_evidence
        self.assertIs(type(original),ClockLeaseEvidence)
        self.assertIs(original.lease,lease)
        self.assertEqual((original.fence_revision,owner.fence_revision),(0,0))
        self.assertEqual(len(original.samples),2)
        self.assertTrue(all(type(row) is ClockSampleEvidence for row in original.samples))
        self.assertIs(original.samples[0].reply,first)
        self.assertIs(original.samples[1].reply,second)
        self.assertEqual(tuple(row.j4_us for row in original.samples),(10_010,10_110))
        pinned=tuple((row.reply.raw_bytes,row.j4_us) for row in original.samples)
        with self.assertRaises(FrozenInstanceError):original.fence_revision=99
        with self.assertRaises(TypeError):ClockLeaseEvidence()
        with self.assertRaises(TypeError):ClockSampleEvidence()
        with self.assertRaises(AttributeError):owner.fence_revision=99
        pending=owner.probe(3,(3).to_bytes(16,'big'),10_200,10_400)
        self.assertIs(owner.lease_evidence,original)
        with self.assertRaisesRegex(HilClockError,'CLOCK_RTT'):
            owner.accept(reply(pending,9204,9205),10_320)
        self.assertIs(owner.lease_evidence,original);self.assertEqual(owner.fence_revision,0)
        sample(owner,4,10_350)
        self.assertIs(owner.lease_evidence,original)
        fifth,fresh=sample(owner,5,10_450)
        issued=owner.lease_evidence
        self.assertIsNot(issued,original);self.assertIs(issued.lease,fresh)
        self.assertEqual(tuple(row.reply.probe.sequence for row in issued.samples),(4,5))
        self.assertEqual(tuple(row.j4_us for row in issued.samples),(10_360,10_460))
        self.assertEqual(issued.fence_revision,0)
        self.assertIs(owner.accept(fifth,20_000),fresh)
        self.assertIs(owner.lease_evidence,issued)
        with self.assertRaisesRegex(HilClockError,'CLOCK_REPLY_CONFLICT'):
            owner.accept(replace(fifth,p3_us=fifth.p3_us+1),10_462)
        self.assertIsNone(owner.lease_evidence);self.assertIsNone(owner.lease)
        self.assertEqual(owner.fence_revision,1)
        self.assertFalse(owner.replace_source(source()));self.assertEqual(owner.fence_revision,1)
        self.assertTrue(owner.replace_source(replace(source(),jetson_session=str(UUID(int=8)))))
        self.assertEqual(owner.fence_revision,2);self.assertIsNone(owner.lease_evidence)
        self.assertEqual(tuple((row.reply.raw_bytes,row.j4_us) for row in original.samples),pinned)
        self.assertEqual((original.lease.j4_us,original.lease.expires_j_us),(10_110,11_100))
