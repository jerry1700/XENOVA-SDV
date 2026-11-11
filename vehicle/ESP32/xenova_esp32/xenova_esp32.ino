// Xenova ESP32 펌웨어 (Arduino-ESP32 3.x, 보드 ESP32 Dev Module).
// 게이트웨이와 USB 시리얼(115200)로 feedback-3 프레임을 주고받는다.
// 지금은 세션(HELLO)과 시각 설치(CLOCK)에 답하고 lease 기한을 타이머로 지킨다.
// 명령 처리와 모터·서보 구동은 아직 없다.
#include <driver/gptimer.h>
#include <esp_app_desc.h>
#include <esp_random.h>

#include "src/device_config.h"
#include "src/feedback3/link.h"
#include "src/quadrature.h"

const int PWM1 = 25, PWM2 = 26;  // 조립 안내서 6장 S01·S03. 구동이 없으니 Low 로 둔다
const int ENC_LA = 18, ENC_LB = 19, ENC_RA = 22, ENC_RB = 23;  // S10·S11

const uint8_t VEHICLE_CONFIG_SHA256[32] = XENOVA_VEHICLE_CONFIG_SHA256;

// 장치 시계이자 cutoff 타이머. 1 µs 씩 세는 64비트 카운터 하나를 같이 쓴다
gptimer_handle_t deviceClock = nullptr;
portMUX_TYPE cutoffMux = portMUX_INITIALIZER_UNLOCKED;
portMUX_TYPE encoderMux = portMUX_INITIALIZER_UNLOCKED;

fb3_framer framer;
fb3_frame frame;
fb3_cutoff cutoff;
fb3_link gatewayLink;
bool linkReady = false;
uint8_t txWire[FB3_MAX_WIRE];

// 엔코더 누계. 32비트이고 넘치면 돌아간다. HELLO_ACK 로 알리는 폭과 같아야 한다
static_assert(XENOVA_ENCODER_BITS == 32, "encoder count is 32-bit");
volatile quadrature encoderL, encoderR;

void IRAM_ATTR onLeft() { quadrature_edge(&encoderL, digitalRead(ENC_LA), digitalRead(ENC_LB)); }
void IRAM_ATTR onRight() { quadrature_edge(&encoderR, digitalRead(ENC_RA), digitalRead(ENC_RB)); }

uint64_t clockNow(void *) {
  uint64_t us = 0;
  gptimer_get_raw_count(deviceClock, &us);
  return us;
}

// 양쪽 누계와 시각을 인터럽트를 막은 채 한 번에 읽는다. 엔코더 인터럽트는 이 코어에서 돈다
bool latchSample(void *, fb3_sample *out) {
  uint64_t us = 0;
  portENTER_CRITICAL(&encoderMux);
  uint32_t left = encoderL.count, right = encoderR.count;
  esp_err_t read = gptimer_get_raw_count(deviceClock, &us);
  portEXIT_CRITICAL(&encoderMux);
  if (read != ESP_OK) return false;
  out->device_us = us;
  out->left_ticks = static_cast<int32_t>(left);
  out->right_ticks = static_cast<int32_t>(right);
  out->quality = FB3_QUALITY_ENCODER;  // 조향 센서가 없다
  out->steering_mrad = 0;
  out->steering_error_mrad = 0;
  return true;
}

bool cutoffSetAlarm(void *, uint64_t atUs) {
  gptimer_alarm_config_t alarm = {};
  alarm.alarm_count = atUs;
  return gptimer_set_alarm_action(deviceClock, &alarm) == ESP_OK;
}

void cutoffClearAlarm(void *) { gptimer_set_alarm_action(deviceClock, nullptr); }
void cutoffLock(void *) { portENTER_CRITICAL_SAFE(&cutoffMux); }
void cutoffUnlock(void *) { portEXIT_CRITICAL_SAFE(&cutoffMux); }

// 타이머 인터럽트. 메인 루프와 상관없이 기한이 지난 lease 를 만료 처리한다
bool IRAM_ATTR onCutoffAlarm(gptimer_handle_t, const gptimer_alarm_event_data_t *, void *) {
  fb3_cutoff_on_alarm(&cutoff);
  return false;
}

bool startClock() {
  gptimer_config_t config = {};
  config.clk_src = GPTIMER_CLK_SRC_DEFAULT;
  config.direction = GPTIMER_COUNT_UP;
  config.resolution_hz = 1000000;
  gptimer_event_callbacks_t callbacks = {};
  callbacks.on_alarm = onCutoffAlarm;
  return gptimer_new_timer(&config, &deviceClock) == ESP_OK
         && gptimer_register_event_callbacks(deviceClock, &callbacks, nullptr) == ESP_OK
         && gptimer_enable(deviceClock) == ESP_OK
         && gptimer_start(deviceClock) == ESP_OK;
}

void startLink() {
  fb3_cutoff_port cutoffPort = {nullptr, clockNow, cutoffSetAlarm, cutoffClearAlarm, cutoffLock, cutoffUnlock};
  fb3_cutoff_init(&cutoff, &cutoffPort);

  fb3_link_config config = {};
  // 켤 때마다 새 session. 하드웨어 난수로 만든 RFC 4122 version 4 UUID 라 NIL 이 될 수 없다
  esp_fill_random(config.device_boot_session, sizeof config.device_boot_session);
  config.device_boot_session[6] = (config.device_boot_session[6] & 0x0F) | 0x40;
  config.device_boot_session[8] = (config.device_boot_session[8] & 0x3F) | 0x80;
  config.capabilities = XENOVA_CAPABILITIES;
  memcpy(config.vehicle_config_sha256, VEHICLE_CONFIG_SHA256, sizeof VEHICLE_CONFIG_SHA256);
  // 빌드가 이미지에 적어 둔 ELF 의 SHA-256
  memcpy(config.firmware_sha256, esp_app_get_description()->app_elf_sha256, sizeof config.firmware_sha256);
  config.encoder_bits = XENOVA_ENCODER_BITS;
  fb3_link_port port = {nullptr, clockNow, latchSample};
  fb3_link_init(&gatewayLink, &config, &port, &cutoff);
}

void setup() {
  pinMode(PWM1, OUTPUT);
  pinMode(PWM2, OUTPUT);
  digitalWrite(PWM1, LOW);
  digitalWrite(PWM2, LOW);
  for (int pin : {ENC_LA, ENC_LB, ENC_RA, ENC_RB}) pinMode(pin, INPUT_PULLUP);
  delay(1);  // 풀업이 자리 잡은 뒤에 읽는다
  // 인터럽트를 걸기 전에 지금 A·B 에서 시작한다. 그러지 않으면 첫 움직임을 거꾸로 셀 수 있다
  quadrature_start(&encoderL, digitalRead(ENC_LA), digitalRead(ENC_LB));
  quadrature_start(&encoderR, digitalRead(ENC_RA), digitalRead(ENC_RB));
  attachInterrupt(digitalPinToInterrupt(ENC_LA), onLeft, CHANGE);
  attachInterrupt(digitalPinToInterrupt(ENC_LB), onLeft, CHANGE);
  attachInterrupt(digitalPinToInterrupt(ENC_RA), onRight, CHANGE);
  attachInterrupt(digitalPinToInterrupt(ENC_RB), onRight, CHANGE);

  // 시계를 못 켜면 아무 프레임에도 답하지 않는다
  linkReady = startClock();
  if (linkReady) startLink();
  fb3_framer_init(&framer);
  Serial.setRxBufferSize(1024);  // begin 전에 정해야 한다
  Serial.begin(115200);
  // 같은 포트로 나간 부팅 문구가 첫 응답 프레임에 붙지 않게 구분자로 끊는다. 빈 조각은 프레임이 아니다
  Serial.write(static_cast<uint8_t>(0));
}

void loop() {
  while (Serial.available() > 0) {
    uint8_t byte = static_cast<uint8_t>(Serial.read());
    uint64_t receivedUs = byte == 0 ? clockNow(nullptr) : 0;  // 프레임의 끝을 읽은 시각
    if (!fb3_framer_push(&framer, byte, &frame) || !linkReady) continue;
    size_t length = fb3_link_receive(&gatewayLink, &frame, receivedUs, txWire, sizeof txWire);
    if (length > 0) Serial.write(txWire, length);
  }
}
