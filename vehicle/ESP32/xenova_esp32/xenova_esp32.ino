// Xenova ESP32 펌웨어 (Arduino-ESP32 3.x, 보드 ESP32 Dev Module).
// 게이트웨이와 USB 시리얼(115200)로 feedback-3 프레임을 주고받는다.
// 지금은 받은 프레임을 검사만 하고 쓰지 않는다. 응답과 모터·서보 구동은 아직 없다.
#include "src/feedback3/wire.h"

const int PWM1 = 25, PWM2 = 26;  // 조립 안내서 6장 S01·S03. 구동이 없으니 Low 로 둔다

fb3_framer framer;
fb3_frame frame;

void setup() {
  pinMode(PWM1, OUTPUT);
  pinMode(PWM2, OUTPUT);
  digitalWrite(PWM1, LOW);
  digitalWrite(PWM2, LOW);
  fb3_framer_init(&framer);
  Serial.setRxBufferSize(1024);  // begin 전에 정해야 한다
  Serial.begin(115200);
}

void loop() {
  while (Serial.available() > 0) {
    if (fb3_framer_push(&framer, static_cast<uint8_t>(Serial.read()), &frame)) {
      // 받은 프레임은 아직 쓰지 않는다
    }
  }
}
