"""feedback-3 고정 vector(JSON)를 C 호스트 시험 데이터(vectors.inc)로 바꾼다.

사용: python3 gen_vectors.py <gateway-feedback-3.json> <vectors.inc>
      python3 gen_vectors.py --wires <gateway-feedback-3.json> <link_vectors.inc>
      python3 gen_vectors.py --tof <gateway-tof-single-1.json> <tof_vectors.inc>

제품 payload 9종의 프레임만 쓴다. 설계 전용 ToF 프레임은 건너뛴다.
--wires 는 세션·시각 설치 프레임 여섯 개와 SENSOR 프레임 네 개의 wire 바이트만 이름으로 꺼낸다(test_link.c 용).
--tof 는 단일 존 ToF vector 의 프레임마다 payload·wire 바이트와 관측 값, CRC32 를 꺼낸다.
"""
import hashlib
import json
import sys

PRODUCT = 'current_product_struct'
# vector symbol → C 이름(fb3_<이름>, FB3_<이름>_ID/SIZE, fb3_unpack_<이름>)
NAMES = {
    'HELLO3': 'hello',
    'ACK3': 'hello_ack',
    'PROBE3': 'clock_probe',
    'CLOCK3': 'clock_reply',
    'INSTALL3': 'clock_install',
    'CLOCK_ACK3': 'clock_ack',
    'SENSOR3': 'sensor',
    'COMMAND3': 'command',
    'FEEDBACK3': 'report',
}
# 한 흐름으로 이어지는 세션·시각 설치 프레임과 그 lease 를 가리키는 SENSOR 프레임 (vector 의 name)
LINK_FRAMES = ('hello3', 'hello_ack3', 'clock_probe3', 'clock_reply3', 'clock_install3', 'clock_ack3',
               'sensor_measured', 'sensor_estimated', 'sensor_encoder_only', 'sensor_signed32_wrap')
ERRORS = {
    'serial boundary/size': 'FB3_FRAME_BOUNDARY_SIZE',
    'serial COBS': 'FB3_FRAME_COBS',
    'short serial frame': 'FB3_FRAME_SHORT',
    'serial length/CRC': 'FB3_FRAME_LENGTH_CRC',
}
INTEGERS = {
    'B': ('uint8_t', 8, False), 'H': ('uint16_t', 16, False),
    'I': ('uint32_t', 32, False), 'Q': ('uint64_t', 64, False),
    'b': ('int8_t', 8, True), 'h': ('int16_t', 16, True),
    'i': ('int32_t', 32, True), 'q': ('int64_t', 64, True),
}


def integer(fmt, value):
    ctype, bits, signed = INTEGERS[fmt]
    low, high = (-(1 << bits - 1), (1 << bits - 1) - 1) if signed else (0, (1 << bits) - 1)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{fmt} 범위 밖: {value!r}')
    if not signed:
        return f'({ctype})UINT64_C({value})'
    if value == -(1 << 63):
        return f'({ctype})(INT64_C({value + 1}) - 1)'
    return f'({ctype})INT64_C({value})'


def field_checks(layout, fields):
    formats = {f['name']: f['format'] for f in layout['fields']}
    if list(fields) != [f['name'] for f in layout['fields']]:
        raise ValueError(f"{layout['symbol']} 필드 순서가 layout 과 다르다")
    for name, value in fields.items():
        fmt = formats[name]
        if fmt.endswith('s'):
            size = int(fmt[:-1])
            if len(value) != size * 2:
                raise ValueError(f'{name} 길이가 {size} B 가 아니다')
            yield f'\tCHECK(same(v.{name}, sizeof v.{name}, "{value}"));'
        else:
            yield f'\tCHECK(v.{name} == {integer(fmt, value)});'


def main(source, target):
    with open(source, 'rb') as f:
        data = f.read()
    vectors = json.loads(data)
    layouts = {layout['symbol']: layout for layout in vectors['layouts'] if layout['origin'] == PRODUCT}
    if set(layouts) != set(NAMES):
        raise ValueError(f'제품 layout 이 9종과 다르다: {sorted(layouts)}')

    out = [
        f'/* gen_vectors.py 가 만든 파일이다. 직접 고치지 않는다. 원본 SHA-256 {hashlib.sha256(data).hexdigest()} */',
        '',
        'static void check_layouts(void)',
        '{',
    ]
    for symbol, name in NAMES.items():
        upper = name.upper()
        out.append(f"\tCHECK(FB3_{upper}_ID == {layouts[symbol]['id']}u);")
        out.append(f"\tCHECK(FB3_{upper}_SIZE == {layouts[symbol]['payload_bytes']}u);")
    out += ['}', '']

    frames = [frame for frame in vectors['frames'] if frame['origin'] == PRODUCT]
    covered = {frame['symbol'] for frame in frames}
    if covered != set(NAMES):
        raise ValueError(f'vector 가 없는 종류: {sorted(set(NAMES) - covered)}')
    for frame in frames:
        name = NAMES[frame['symbol']]
        out += [
            f"static void fields_{frame['name']}(const uint8_t *p, size_t n)",
            '{',
            f'\tstruct fb3_{name} v;',
            f'\tuint8_t out[FB3_{name.upper()}_SIZE];',
            '\tmemset(&v, 0xa5, sizeof v);',
            f'\tCHECK(fb3_unpack_{name}(p, n, &v));',
            *field_checks(layouts[frame['symbol']], frame['fields']),
            f'\tCHECK(fb3_pack_{name}(&v, out, sizeof out) == n && memcmp(out, p, n) == 0);',
            '}',
            '',
        ]

    out.append('static const struct frame_case FRAMES[] = {')
    for frame in frames:
        out.append(f"\t{{\"{frame['name']}\", {frame['id']}u, \"{frame['payload_hex']}\", "
                   f"\"{frame['raw_hex']}\", \"{frame['wire_hex']}\", fields_{frame['name']}}},")
    out += ['};', '']

    out.append('static const struct malformed_case MALFORMED[] = {')
    for case in vectors['malformed']:
        out.append(f"\t{{\"{case['name']}\", \"{case['wire_hex']}\", {ERRORS[case['error']]}}},")
    out += ['};', '']

    out.append('static const struct cobs_case COBS[] = {')
    for case in vectors['cobs_checks']:
        out.append(f"\t{{\"{case['raw_hex']}\", \"{case['encoded_hex']}\"}},")
    out += ['};', '']

    crc = vectors['crc16_check']
    out += [f"static const char CRC_ASCII[] = \"{crc['ascii']}\";",
            f"static const uint16_t CRC_EXPECTED = 0x{crc['expected_hex']};", '']

    with open(target, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(out))


def wires(source, target):
    with open(source, 'rb') as f:
        data = f.read()
    frames = {frame['name']: frame for frame in json.loads(data)['frames'] if frame['origin'] == PRODUCT}
    missing = [name for name in LINK_FRAMES if name not in frames]
    if missing:
        raise ValueError(f'vector 에 없는 프레임: {missing}')
    out = [f'/* gen_vectors.py --wires 가 만든 파일이다. 직접 고치지 않는다. 원본 SHA-256 {hashlib.sha256(data).hexdigest()} */']
    out += [f"static const char VEC_{name.upper()}[] = \"{frames[name]['wire_hex']}\";" for name in LINK_FRAMES]
    with open(target, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(out) + '\n')


def tof(source, target):
    with open(source, 'rb') as f:
        data = f.read()
    vectors = json.loads(data)
    if vectors['format'] != '<16s16sQQI16sQB16sII32sBBHHBBI':
        raise ValueError(f"ToF 형식 문자열이 다르다: {vectors['format']}")
    out = [f'/* gen_vectors.py --tof 가 만든 파일이다. 직접 고치지 않는다. 원본 SHA-256 {hashlib.sha256(data).hexdigest()} */',
           f"static const uint16_t TOF_ID = {integer('H', vectors['opcode'])};",
           f"static const uint16_t TOF_CAPABILITY = {integer('H', vectors['capability'])};",
           f"static const size_t TOF_PAYLOAD_BYTES = {vectors['payload_bytes']}u, TOF_CRC_BYTES = {vectors['source_crc_bytes']}u, "
           f"TOF_WIRE_BYTES = {vectors['maximum_wire_bytes']}u;",
           'static const struct tof_case TOF_FRAMES[] = {']
    for frame in vectors['frames']:
        if frame['source_crc_body_hex'] != frame['payload_hex'][:2 * vectors['source_crc_bytes']]:
            raise ValueError(f"{frame['name']}: CRC 가 덮는 부분이 payload 의 앞부분이 아니다")
        out.append(f"\t{{\"{frame['name']}\", {integer('B', frame['sensor_id'])}, {integer('Q', frame['source_sample'])}, "
                   f"{integer('Q', frame['source_us'])}, {integer('H', frame['distance_mm'])}, {integer('B', frame['status'])}, "
                   f"{integer('B', frame['quality'])}, {integer('I', frame['source_crc32'])}, "
                   f"\"{frame['payload_hex']}\", \"{frame['wire_hex']}\"}},")
    out += ['};', '']
    with open(target, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(out))


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--wires':
        wires(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 4 and sys.argv[1] == '--tof':
        tof(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 3:
        main(sys.argv[1], sys.argv[2])
    else:
        sys.exit(__doc__)
