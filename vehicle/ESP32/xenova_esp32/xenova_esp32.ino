// Xenova ESP32 펌웨어 (Arduino-ESP32 3.x, 보드 ESP32 Dev Module).
// 게이트웨이와 USB 시리얼로 feedback-3 프레임을 주고받는다.
// 세션(HELLO)과 시각 설치(CLOCK)에 답하고, lease 가 살아 있는 동안 엔코더 표본(SENSOR)을 20 ms 마다 보낸다.
// DRIVE·STOP·HOLD_STEER 명령을 모터와 서보에 걸고 REPORT 로 알리며, 명령의 기한이 지나면 타이머가 모터 출력을 끊는다.
// 앞·뒤 ToF 거리 센서(VL53L0X)를 따로 도는 task 에서 읽어 TOF_SAMPLE 로 보낸다.
// 라이브러리: VL53L0X by Pololu 1.3.1 (Library Manager)
#include <VL53L0X.h>
#include <Wire.h>
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
// ToF 는 두 센서가 I2C 두 선을 같이 쓴다. 22 번은 오른쪽 엔코더가 쓰고 있어 SCL 은 17 이다
const int TOF_SDA = 21, TOF_SCL = 17;
const int TOF_XSHUT[FB3_TOF_SENSORS] = {16, 4};  // 앞, 뒤

const uint32_t MOTOR_HZ = 20000;  // MDD10A 최대 20 kHz
const uint8_t MOTOR_BITS = 10;
// 속도를 PWM 으로 바꾸는 값. 엔코더로 되먹이지 않는 개루프다. 바닥에서 3초씩 달려 엔코더로 잰 두 점
// (듀티 17.4 % 에 0.145 m/s, 26.1 % 에 0.256 m/s)을 이은 직선이다. 듀티 6 % 까지는 바퀴가 구르지 않는다.
// 배터리 전압과 바닥에 따라 달라진다
const int32_t FULL_DUTY_MMPS = 1276;
const uint32_t MOTOR_START_DUTY = 62;  // 1023 가운데
// 조향(앞바퀴 각)을 서보 펄스로 바꾸는 값. 한계는 조립 안내서 단계 K 에서 찾았고, 나머지는 바닥에서 조향마다
// 3초씩 달려 양쪽 엔코더의 차이로 잰 값이다(조립 안내서 9.2).
//  - 바퀴 1 rad 에 1170 us. 서보 팔이 도는 각의 0.545 배만 바퀴가 꺾인다
//  - 가운데 1523 us. 1500 us 에서는 바퀴가 왼쪽으로 1.2도 틀어져 있다
//  - 유격 66 us. 조향 방향을 바꾸면 바퀴가 3.3도 늦게 따라와서, 움직이는 쪽으로 그만큼 더 보낸다
const int32_t SERVO_CENTER_US = 1523, SERVO_MIN_US = 900, SERVO_MAX_US = 2100;
const int32_t SERVO_US_PER_RAD = 1170;
const int32_t SERVO_BACKLASH_US = 66;
// 이보다 작은 조향 변화에는 서보를 움직이지 않는다. 명령이 조금씩 흔들릴 때 유격 보정이 좌우로 떨지 않게 한다
const int32_t STEER_STEP_MRAD = 10;
const uint32_t SERVO_PERIOD_US = 20000;  // 50 Hz

// 켜질 때 두 센서의 주소가 같아서(0x29), XSHUT 으로 하나씩 켜며 이 주소로 옮긴다
const uint8_t TOF_ADDRESS[FB3_TOF_SENSORS] = {0x30, 0x31};
const uint32_t TOF_BUDGET_US = 33000;  // 측정 하나에 쓰는 시간 (센서의 timing budget)
const uint32_t TOF_PERIOD_MS = 100;    // 센서가 측정을 시작하는 간격
const uint32_t TOF_POLL_MS = 5;        // 측정이 끝났는지 보는 간격
const uint32_t TOF_RETRY_MS = 1000;    // 센서가 답하지 않으면 이만큼 뒤에 다시 초기화해 본다

const uint8_t VEHICLE_CONFIG_SHA256[32] = XENOVA_VEHICLE_CONFIG_SHA256;
const uint8_t TOF_FRONT_SHA256[32] = XENOVA_TOF_FRONT_SHA256;
const uint8_t TOF_REAR_SHA256[32] = XENOVA_TOF_REAR_SHA256;

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
int32_t steerHeldMrad = 0;  // 서보에 걸어 둔 조향
int32_t steerSide = 0;      // 서보가 마지막으로 움직인 쪽. 왼쪽 +1, 오른쪽 -1, 아직 움직이지 않았으면 0

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
  uint32_t duty = speed ? MOTOR_START_DUTY + static_cast<uint32_t>(speed) * top / FULL_DUTY_MMPS : 0;
  int32_t change = steeringMrad - steerHeldMrad;
  if (steerSide == 0 || change >= STEER_STEP_MRAD || change <= -STEER_STEP_MRAD) {
    if (change != 0) steerSide = change > 0 ? 1 : -1;
    steerHeldMrad = steeringMrad;
  }
  // 조향은 왼쪽이 + 이고(MQTT 계약 v1), 이 차의 서보는 펄스가 짧을수록 왼쪽이다(조립 안내서 9.2)
  int32_t pulseUs = SERVO_CENTER_US - steerHeldMrad * SERVO_US_PER_RAD / 1000 - steerSide * SERVO_BACKLASH_US;
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

// ToF. I2C 는 센서가 답하지 않으면 수십 ms 를 기다리므로 메인 루프에서 읽지 않는다. 다른 코어에서 도는 task 가
// 읽어서 센서마다 가장 새 관측 하나를 여기에 두고, 메인 루프는 그것을 집어 가기만 한다
portMUX_TYPE tofMux = portMUX_INITIALIZER_UNLOCKED;
fb3_tof_reading tofLatest[FB3_TOF_SENSORS];
bool tofFresh[FB3_TOF_SENSORS] = {false, false};  // 아직 집어 가지 않은 관측이 있다

bool takeTof(void *, unsigned sensor, fb3_tof_reading *out) {
  bool fresh = false;
  portENTER_CRITICAL(&tofMux);
  if (tofFresh[sensor]) {
    *out = tofLatest[sensor];
    tofFresh[sensor] = false;
    fresh = true;
  }
  portEXIT_CRITICAL(&tofMux);
  return fresh;
}

// 센서의 native 상태(RESULT_RANGE_STATUS 의 bit 3..6)를 wire status 로 바꾼다. ST 의 VL53L0X API 가 쓰는 구분이다
uint8_t tofStatus(uint8_t native, uint16_t distanceMm) {
  switch ((native & 0x78) >> 3) {
    case 11:  // 측정 완료
      // 8190 이상은 잴 물체가 없다는 값이다. 0 은 어느 쪽인지 알 수 없어 물체 없음으로 치지 않는다
      return distanceMm >= 8190 ? FB3_TOF_OUT_OF_RANGE : distanceMm >= 1 ? FB3_TOF_VALID : FB3_TOF_BAD_RETURN;
    case 4:   // 돌아온 신호가 약함
      return FB3_TOF_NO_TARGET;
    case 6:
    case 9:   // 위상이 범위 밖 (거리가 측정 범위를 넘음)
      return FB3_TOF_OUT_OF_RANGE;
    case 8:
    case 10:  // 최소 거리 검사 실패
      return FB3_TOF_BAD_RETURN;
    case 1:
    case 2:
    case 3:   // VCSEL·VHV 같은 센서 안의 고장
      return FB3_TOF_SENSOR_ERROR;
    default:
      return FB3_TOF_UNKNOWN;
  }
}

VL53L0X tofSensor[FB3_TOF_SENSORS];

// 센서 하나를 껐다 켜서 주소를 옮기고 연속 측정을 건다. 답하지 않으면 false
bool startTof(unsigned sensor) {
  digitalWrite(TOF_XSHUT[sensor], LOW);
  delay(10);
  digitalWrite(TOF_XSHUT[sensor], HIGH);
  delay(10);
  tofSensor[sensor] = VL53L0X();  // 주소를 기본값 0x29 로 되돌린다
  tofSensor[sensor].setBus(&Wire);
  tofSensor[sensor].setTimeout(50);
  if (!tofSensor[sensor].init()) return false;
  tofSensor[sensor].setAddress(TOF_ADDRESS[sensor]);
  if (tofSensor[sensor].last_status != 0 || !tofSensor[sensor].setMeasurementTimingBudget(TOF_BUDGET_US)) return false;
  tofSensor[sensor].startContinuous(TOF_PERIOD_MS);
  return tofSensor[sensor].last_status == 0;
}

void tofTask(void *) {
  bool up[FB3_TOF_SENSORS] = {false, false};
  uint32_t retryAtMs[FB3_TOF_SENSORS] = {0, 0};
  uint32_t generation[FB3_TOF_SENSORS] = {0, 0};
  uint8_t instance[FB3_TOF_SENSORS][16];
  uint64_t lookedUs[FB3_TOF_SENSORS] = {0, 0};  // 그 센서의 측정이 끝났는지 마지막으로 본 시각

  // 리셋 뒤에도 센서 전원은 그대로라 옮긴 주소가 남아 있다. 둘 다 끄고 시작한다
  for (unsigned i = 0; i < FB3_TOF_SENSORS; i++) {
    pinMode(TOF_XSHUT[i], OUTPUT);
    digitalWrite(TOF_XSHUT[i], LOW);
  }
  Wire.begin(TOF_SDA, TOF_SCL, 400000);
  Wire.setTimeOut(20);
  for (;;) {
    for (unsigned i = 0; i < FB3_TOF_SENSORS; i++) {
      if (!up[i]) {
        if (static_cast<int32_t>(millis() - retryAtMs[i]) < 0) continue;
        up[i] = startTof(i);
        if (up[i]) {
          // 초기화할 때마다 새 instance 와 더 큰 generation. RFC 4122 version 4 UUID 라 NIL 이 될 수 없다
          generation[i]++;
          esp_fill_random(instance[i], sizeof instance[i]);
          instance[i][6] = (instance[i][6] & 0x0F) | 0x40;
          instance[i][8] = (instance[i][8] & 0x3F) | 0x80;
          lookedUs[i] = clockNow(nullptr);
        } else {
          digitalWrite(TOF_XSHUT[i], LOW);
          retryAtMs[i] = millis() + TOF_RETRY_MS;
        }
        continue;
      }
      // 새 측정이 끝났는지 본다. 끝났으면 상태와 거리를 읽고 다음 측정을 위해 표시를 지운다
      bool ready = (tofSensor[i].readReg(VL53L0X::RESULT_INTERRUPT_STATUS) & 0x07) != 0;
      uint64_t seenUs = clockNow(nullptr);
      uint64_t sinceUs = seenUs - lookedUs[i];
      lookedUs[i] = seenUs;
      uint8_t native = 0;
      uint16_t distanceMm = 0;
      if (ready && tofSensor[i].last_status == 0) {
        native = tofSensor[i].readReg(VL53L0X::RESULT_RANGE_STATUS);
        if (tofSensor[i].last_status == 0) distanceMm = tofSensor[i].readReg16Bit(VL53L0X::RESULT_RANGE_STATUS + 10);
        if (tofSensor[i].last_status == 0) tofSensor[i].writeReg(VL53L0X::SYSTEM_INTERRUPT_CLEAR, 0x01);
      }
      if (tofSensor[i].last_status != 0) {
        // 센서가 답하지 않는다. 끄고 나중에 다시 초기화한다
        up[i] = false;
        digitalWrite(TOF_XSHUT[i], LOW);
        retryAtMs[i] = millis() + TOF_RETRY_MS;
        continue;
      }
      // 다른 센서를 초기화하느라 오래 못 봤으면 어느 측정의 값인지 알 수 없다. 그 관측은 내지 않는다
      if (!ready || sinceUs > TOF_PERIOD_MS * 1000) continue;
      // 측정은 앞서 본 때와 지금 사이에 끝났고, 끝나기 전 TOF_BUDGET_US 동안 이뤄졌다.
      // 그 구간의 가운데를 관측 시각으로, 구간의 절반에 읽는 시간의 여유를 더해 오차로 낸다
      fb3_tof_reading reading = {};
      uint32_t span = TOF_BUDGET_US + static_cast<uint32_t>(sinceUs);
      reading.device_us = seenUs - span / 2;
      reading.time_error_us = span / 2 + 1000;
      memcpy(reading.instance, instance[i], sizeof reading.instance);
      reading.generation = generation[i];
      reading.status = tofStatus(native, distanceMm);
      reading.distance_mm = distanceMm;
      portENTER_CRITICAL(&tofMux);
      tofLatest[i] = reading;
      tofFresh[i] = true;
      portEXIT_CRITICAL(&tofMux);
    }
    vTaskDelay(pdMS_TO_TICKS(TOF_POLL_MS));
  }
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
  config.tof[0].config_revision = XENOVA_TOF_FRONT_REVISION;
  memcpy(config.tof[0].calibration_sha256, TOF_FRONT_SHA256, sizeof TOF_FRONT_SHA256);
  config.tof[1].config_revision = XENOVA_TOF_REAR_REVISION;
  memcpy(config.tof[1].calibration_sha256, TOF_REAR_SHA256, sizeof TOF_REAR_SHA256);
  config.proximity_stop_mm = XENOVA_PROXIMITY_STOP_MM;
  config.proximity_clear_mm = XENOVA_PROXIMITY_CLEAR_MM;
  config.proximity_stale_us = XENOVA_PROXIMITY_STALE_US;
  fb3_link_port port = {nullptr, clockNow, latchSample, driveOutput, estopPressed, takeTof};
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
  if (linkReady) {
    startLink();
    // 메인 루프는 코어 1 에서 돈다. ToF 는 코어 0 에서 읽는다. 센서가 없으면 관측이 오지 않을 뿐이다
    xTaskCreatePinnedToCore(tofTask, "tof", 4096, nullptr, 1, nullptr, 0);
  }
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
