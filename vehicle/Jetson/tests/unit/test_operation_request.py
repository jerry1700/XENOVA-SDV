"""명시 local 요청의 원문·형식만 검산한다. 설정 수락이나 Pi 권한은 만들지 않는다."""

from dataclasses import FrozenInstanceError
from hashlib import sha256
import json
import unittest

from test_mapper_compute import identifier
from xenova_vehicle.runtime.operation_request import OperationRequest,OperationRequestError


def fields(**changes):
    value=dict(profile='jetson-operation-request-1',service_instance=identifier(1),request_id=identifier(2),
        action='STANDBY',issued_j_us='1000',expires_j_us='5001000')
    value.update(changes);return value


def request_raw(**changes):return json.dumps(fields(**changes),sort_keys=True,separators=(',',':')).encode('ascii')


class OperationRequestTests(unittest.TestCase):
    def test_original_ascii_sha_and_independent_semantic_copy_are_not_aliased(self):
        raw=json.dumps(fields(),indent=1).encode('ascii');request=OperationRequest(raw)
        self.assertIs(request.raw_bytes,raw);self.assertEqual(request.sha256,sha256(raw).hexdigest())
        self.assertNotEqual(request.canonical_bytes,raw)
        alternate=OperationRequest(request.canonical_bytes)
        self.assertEqual(alternate.canonical_bytes,request.canonical_bytes)
        self.assertNotEqual(alternate.sha256,request.sha256)
        copied=request.fields;copied['action']='RUN'
        self.assertEqual(request.action,'STANDBY');self.assertEqual(request.fields['action'],'STANDBY')
        self.assertEqual((request.issued_us,request.expires_us),(1000,5001000))
        with self.assertRaises(FrozenInstanceError):request.action='RUN'

    def test_exact_shape_uuid_decimal_action_and_lifetime_domains(self):
        bad=[fields(profile='other'),fields(action='standby'),fields(action=True),fields(issued_j_us=1000),
            fields(issued_j_us=True),fields(issued_j_us='01000'),fields(expires_j_us='1000'),
            fields(expires_j_us='5001001'),fields(expires_j_us=str(2**64)),
            fields(service_instance='ABCDEFAB-1234-4000-8000-000000000001'),
            fields(request_id='00000000-0000-0000-0000-000000000000')]
        missing=fields();missing.pop('action');bad.extend((missing,dict(fields(),extra=None)))
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(OperationRequestError):OperationRequest(json.dumps(value).encode('ascii'))
        with self.assertRaises(OperationRequestError):OperationRequest(request_raw().replace(b'"action":"STANDBY"',b'"action":"RUN","action":"STANDBY"'))

    def test_bounded_invalid_nested_json_is_a_request_error_not_actor_recursion(self):
        for raw in (b'',b' '*4097,b'['*1500+b']'*1500,b'{"x":NaN}',b'\xff',bytearray(request_raw())):
            with self.subTest(size=len(raw)):
                with self.assertRaises(OperationRequestError):OperationRequest(raw)


if __name__=='__main__':unittest.main()
