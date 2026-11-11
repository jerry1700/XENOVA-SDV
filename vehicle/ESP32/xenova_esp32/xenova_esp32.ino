// Xenova ESP32 펌웨어 (Arduino-ESP32 3.x, 보드 ESP32 Dev Module).
// 게이트웨이와 USB 시리얼로 feedback-3 프레임을 주고받는다.
// 세션(HELLO)과 시각 설치(CLOCK)에 답하고, lease 가 살아 있는 동안 엔코더 표본(SENSOR)을 20 ms 마다 보낸다.
// DRIVE·STOP 명령을 모터와 서보에 걸고 REPORT 로 알리며, 명령의 기한이 지나면 타이머가 모터 출력을 끊는다.
#include <driver/gptimer.h>
#include <esp_app_desc.h>
#include <esp_random.h>

#include "src/device_config.h"
#include "src/feedback3/link.h"
#include "src/quadrature.h"

// 조립 안내서 6장의 연결표
const int PWM1 = 25, DIR1 = 32, PWM2 = 26, DIR2 = 33;
const int SERVO = 27, ESTOP = 13;
const int ENC_LA = 18, ENC_LB = 19, ENC_RA = 22, ENC_RB = 23;

const uint32_t MOTOR_HZ = 20000;  // MDD10A 최대 20 kHz
const uint8_t MOTOR_BITS = 10;
// 속도를 PWM 으로 바꾸는 값. 엔코더로 되먹이지 않는 개루프다.
// 모터의 무부하 320 rpm 과 바퀴 둘레 217 mm 로 어림했고, 바퀴를 띄운 시험에서 잰 값으로 바꿔야 한다
const int32_t FULL_DUTY_MMPS = 1150;
// 조향을 서보 펄스로 바꾸는 값. 한계는 조립 안내서 단계 K 에서 찾은 것이고, 기울기와 부호는 맞추기 전이다
const int32_t SERVO_CENTER_US = 1500, SERVO_MIN_US = 900, SERVO_MAX_US = 2100;
const int32_t SERVO_US_PER_RAD = 637;
// LEDC 는 앞서 쓴 값이 자리 잡기 전에 다시 쓰면 그때까지 멈춰서 기다린다. 이 보드에서 재 보니 PWM 두 주기였고,
// 50 Hz 인 서보 채널에서는 40 ms 다. 그래서 서보에는 이 간격을 두고 loop 에서만 쓴다
const uint64_t SERVO_WRITE_GAP_US = 50000;

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

// 모터 PWM 을 0 으로 내린다. 타이머 인터럽트 안에서도 불린다. 서보는 마지막 자리에 둔다
void cutOutput(void *) {
  ledcWrite(PWM1, 0);
  ledcWrite(PWM2, 0);
}

int32_t servoWantedUs = 0;   // 서보에 낼 펄스. 0 이면 아직 내지 않는다
int32_t servoWrittenUs = 0;  // 서보 채널에 마지막으로 쓴 펄스
uint64_t servoWrittenAtUs = 0;

// 속도를 모터에 걸고 조향은 낼 펄스만 적어 둔다. cutoff 의 lock 을 쥔 채, 곧 타이머 인터럽트를 막은 채 불린다.
// 여기서 서보 채널에 쓰면 길게는 40 ms 동안 기한의 alarm 이 밀린다
bool driveOutput(void *, int16_t velocityMmps, int16_t steeringMrad) {
  bool forward = velocityMmps >= 0;
  int32_t speed = forward ? velocityMmps : -static_cast<int32_t>(velocityMmps);
  uint32_t top = (1u << MOTOR_BITS) - 1;
  uint32_t duty = static_cast<uint32_t>(speed) * top / FULL_DUTY_MMPS;
  int32_t pulseUs = SERVO_CENTER_US + steeringMrad * SERVO_US_PER_RAD / 1000;
  servoWantedUs = constrain(pulseUs, SERVO_MIN_US, SERVO_MAX_US);
  digitalWrite(DIR1, forward ? LOW : HIGH);
  digitalWrite(DIR2, forward ? HIGH : LOW);  // 마주 보고 달린 모터라 반대다
  bool ok = ledcWrite(PWM1, duty > top ? top : duty);
  return ledcWrite(PWM2, duty > top ? top : duty) && ok;
}

// 적어 둔 펄스가 바뀌었으면 서보 채널에 쓴다. 앞서 쓴 지 SERVO_WRITE_GAP_US 가 지난 뒤에만 쓴다
void serviceServo() {
  if (servoWantedUs == servoWrittenUs) return;
  uint64_t now = clockNow(nullptr);
  if (servoWrittenUs != 0 && now - servoWrittenAtUs < SERVO_WRITE_GAP_US) return;
  ledcWrite(SERVO, static_cast<uint32_t>(servoWantedUs) * 16383 / 20000);  // 50 Hz, 14비트
  servoWrittenUs = servoWantedUs;
  servoWrittenAtUs = now;
}

// NC 접점을 13번과 GND 사이에 잇는다. 누르거나 선이 빠지면 HIGH
bool estopPressed(void *) { return digitalRead(ESTOP) == HIGH; }

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
  fb3_cutoff_port cutoffPort = {nullptr, clockNow, cutoffSetAlarm, cutoffClearAlarm, cutoffLock, cutoffUnlock, cutOutput};
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
  config.um_per_tick = XENOVA_UM_PER_TICK;
  config.j_apply_us = XENOVA_J_APPLY_US;
  config.max_velocity_mmps = XENOVA_MAX_VELOCITY_MMPS;
  config.max_steering_mrad = XENOVA_MAX_STEERING_MRAD;
  fb3_link_port port = {nullptr, clockNow, latchSample, driveOutput, estopPressed};
  fb3_link_init(&gatewayLink, &config, &port, &cutoff);
}

void setup() {
  // 켜지면 모터 PWM 은 0 이고 서보에는 펄스를 내지 않는다. 첫 DRIVE 가 올 때 서보가 움직인다
  pinMode(DIR1, OUTPUT);
  pinMode(DIR2, OUTPUT);
  digitalWrite(DIR1, LOW);
  digitalWrite(DIR2, LOW);
  ledcAttach(PWM1, MOTOR_HZ, MOTOR_BITS);
  ledcAttach(PWM2, MOTOR_HZ, MOTOR_BITS);
  ledcAttach(SERVO, 50, 14);
  ledcWrite(PWM1, 0);
  ledcWrite(PWM2, 0);
  ledcWrite(SERVO, 0);
  pinMode(ESTOP, INPUT_PULLUP);
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
  // 송신 버퍼가 없으면 앞 프레임이 다 나가기 전의 write 가 그만큼 loop 를 세운다
  Serial.setTxBufferSize(1024);
  Serial.begin(XENOVA_SERIAL_BAUD);
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
  if (!linkReady) return;
  serviceServo();
  // 미뤄 둔 CLOCK_REPLY 나 주기 표본을 보낼 때가 됐으면 보낸다
  size_t length = fb3_link_poll(&gatewayLink, txWire, sizeof txWire);
  if (length > 0) Serial.write(txWire, length);
}
