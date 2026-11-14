/* SPDX-License-Identifier: Apache-2.0 */
#include <xenova/auto_input_fault.h>
#include <zephyr/kernel.h>
static struct k_spinlock lock;
static int64_t expires_at_ms;
static bool active_locked(int64_t now){if(expires_at_ms&&now>=expires_at_ms)expires_at_ms=0;return expires_at_ms!=0;}
int drive_auto_input_fault_start(uint32_t duration_s,struct drive_auto_input_fault_receipt *out){
 if(!duration_s||duration_s>120)return -ERANGE;int64_t now=k_uptime_get();k_spinlock_key_t key=k_spin_lock(&lock);
 if(active_locked(now)){k_spin_unlock(&lock,key);return -EBUSY;}
 if(now<0||now>INT64_MAX-(int64_t)duration_s*1000){k_spin_unlock(&lock,key);return -ERANGE;}
 expires_at_ms=now+(int64_t)duration_s*1000;
 if(out)*out=(struct drive_auto_input_fault_receipt){now,expires_at_ms};k_spin_unlock(&lock,key);return 0;
}
void drive_auto_input_fault_stop(void){k_spinlock_key_t key=k_spin_lock(&lock);expires_at_ms=0;k_spin_unlock(&lock,key);}
bool drive_auto_input_fault_active(void){k_spinlock_key_t key=k_spin_lock(&lock);bool active=active_locked(k_uptime_get());k_spin_unlock(&lock,key);return active;}
