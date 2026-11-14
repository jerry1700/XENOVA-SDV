// Xenova ESP32 펌웨어 (Arduino-ESP32 3.x, 보드 ESP32 Dev Module).
// 게이트웨이와 USB 시리얼로 feedback-3 프레임을 주고받는다.
// 세션(HELLO)과 시각 설치(CLOCK)에 답하고, lease 가 살아 있는 동안 엔코더 표본(SENSOR)을 20 ms 마다 보낸다.
// DRIVE·STOP·HOLD_STEER 명령을 모터와 서보에 걸고 REPORT 로 알리며, 명령의 기한이 지나면 타이머가 모터 출력을 끊는다.
#include <driver/gptimer.h>
#include <driver/mcpwm_prelude.h>
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
const uint32_t SERVO_PERIOD_US = 20000;  // 50 Hz

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

// 서보 펄스는 MCPWM 으로 낸다. 폭을 바꾸는 것은 비교값 레지스터를 쓰는 일이라 기다리지 않고, 다음 주기부터 그 폭으로
// 나간다. LEDC 는 앞서 쓴 값이 자리 잡기 전에 다시 쓰면 PWM 두 주기(50 Hz 에서 40 ms)를 멈춰 기다려서, 타이머
// 인터럽트를 막은 채로는 쓸 수 없다
mcpwm_cmpr_handle_t servoCompare = nullptr;
mcpwm_gen_handle_t servoGenerator = nullptr;
bool servoOn = false;  // 펄스를 내고 있다. 켜진 뒤 첫 명령 전에는 핀을 LOW 로 잡아 둔다

bool startServo() {
  mcpwm_timer_handle_t timer = nullptr;
  mcpwm_oper_handle_t oper = nullptr;
  mcpwm_timer_config_t timerConfig = {};
  timerConfig.group_id = 0;
  timerConfig.clk_src = MCPWM_TIMER_CLK_SRC_DEFAULT;
  timerConfig.resolution_hz = 1000000;  // 비교값이 곧 us
  timerConfig.count_mode = MCPWM_TIMER_COUNT_MODE_UP;
  timerConfig.period_ticks = SERVO_PERIOD_US;
  mcpwm_operator_config_t operConfig = {};
  operConfig.group_id = 0;
  mcpwm_comparator_config_t compareConfig = {};
  compareConfig.flags.update_cmp_on_tez = true;  // 새 폭은 주기의 처음에 바뀐다. 펄스 중간에 바뀌지 않는다
  mcpwm_generator_config_t generatorConfig = {};
  generatorConfig.gen_gpio_num = SERVO;
  mcpwm_gen_timer_event_action_t rise = {};
  rise.direction = MCPWM_TIMER_DIRECTION_UP;
  rise.event = MCPWM_TIMER_EVENT_EMPTY;
  rise.action = MCPWM_GEN_ACTION_HIGH;
  mcpwm_gen_compare_event_action_t fall = {};
  fall.direction = MCPWM_TIMER_DIRECTION_UP;
  fall.action = MCPWM_GEN_ACTION_LOW;
  if (mcpwm_new_timer(&timerConfig, &timer) != ESP_OK || mcpwm_new_operator(&operConfig, &oper) != ESP_OK
      || mcpwm_operator_connect_timer(oper, timer) != ESP_OK
      || mcpwm_new_comparator(oper, &compareConfig, &servoCompare) != ESP_OK
      || mcpwm_new_generator(oper, &generatorConfig, &servoGenerator) != ESP_OK) {
    return false;
  }
  fall.comparator = servoCompare;
  return mcpwm_comparator_set_compare_value(servoCompare, SERVO_CENTER_US) == ESP_OK
         && mcpwm_generator_set_action_on_timer_event(servoGenerator, rise) == ESP_OK
         && mcpwm_generator_set_action_on_compare_event(servoGenerator, fall) == ESP_OK
         && mcpwm_generator_set_force_level(servoGenerator, 0, true) == ESP_OK
         && mcpwm_timer_enable(timer) == ESP_OK
         && mcpwm_timer_start_stop(timer, MCPWM_TIMER_START_NO_STOP) == ESP_OK;
}

// 속도를 모터에, 조향을 서보에 건다. true 를 돌려줄 때는 두 출력이 모두 그 값으로 바뀌어 있다.
// cutoff 의 lock 을 쥔 채, 곧 타이머 인터럽트를 막은 채 불리므로 기다리는 호출을 넣으면 안 된다
bool driveOutput(void *, int16_t velocityMmps, int16_t steeringMrad) {
  bool forward = velocityMmps >= 0;
  int32_t speed = forward ? velocityMmps : -static_cast<int32_t>(velocityMmps);
  uint32_t top = (1u << MOTOR_BITS) - 1;
  uint32_t duty = static_cast<uint32_t>(speed) * top / FULL_DUTY_MMPS;
  // 조향은 왼쪽이 + 이고(MQTT 계약 v1), 이 차의 서보는 펄스가 짧을수록 왼쪽이다(조립 안내서 9.2)
  int32_t pulseUs = SERVO_CENTER_US - steeringMrad * SERVO_US_PER_RAD / 1000;
  pulseUs = constrain(pulseUs, SERVO_MIN_US, SERVO_MAX_US);
  bool ok = mcpwm_comparator_set_compare_value(servoCompare, static_cast<uint32_t>(pulseUs)) == ESP_OK;
  if (ok && !servoOn) {
    ok = mcpwm_generator_set_force_level(servoGenerator, -1, true) == ESP_OK;
    servoOn = ok;
  }
  digitalWrite(DIR1, forward ? LOW : HIGH);
  digitalWrite(DIR2, forward ? HIGH : LOW);  // 마주 보고 달린 모터라 반대다
  ok = ledcWrite(PWM1, duty > top ? top : duty) && ok;
  return ledcWrite(PWM2, duty > top ? top : duty) && ok;
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
  // 켜지면 모터 PWM 은 0 이고 서보에는 펄스를 내지 않는다. 첫 DRIVE 나 HOLD_STEER 가 올 때 서보가 움직인다
  pinMode(DIR1, OUTPUT);
  pinMode(DIR2, OUTPUT);
  digitalWrite(DIR1, LOW);
  digitalWrite(DIR2, LOW);
  ledcAttach(PWM1, MOTOR_HZ, MOTOR_BITS);
  ledcAttach(PWM2, MOTOR_HZ, MOTOR_BITS);
  ledcWrite(PWM1, 0);
  ledcWrite(PWM2, 0);
  bool servoReady = startServo();
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

  // 시계나 서보 출력을 못 켜면 아무 프레임에도 답하지 않는다
  linkReady = servoReady && startClock();
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
  // 미뤄 둔 CLOCK_REPLY 나 주기 표본을 보낼 때가 됐으면 보낸다
  size_t length = fb3_link_poll(&gatewayLink, txWire, sizeof txWire);
  if (length > 0) Serial.write(txWire, length);
}
