/* SPDX-License-Identifier: Apache-2.0 */
/* Handoff from the MQTT owner to the current AUTO worker's safe point. */
#include <errno.h>
#include <zephyr/kernel.h>
#include <xenova/auto_exit.h>

/* Provisional LAB bound for observing a safe-point exit. */
#define AUTO_EXIT_TIMEOUT_MS 1000

static struct k_spinlock lock;
static struct k_sem *worker_wake;
static bool requested, exiting;
static int outcome;
static int64_t deadline_ms;

void drive_auto_exit_bind(struct k_sem *wake)
{
	k_spinlock_key_t key = k_spin_lock(&lock);
	worker_wake = wake;
	k_spin_unlock(&lock, key);
}

void drive_auto_exit_unbind(void)
{
	k_spinlock_key_t key = k_spin_lock(&lock);
	worker_wake = NULL;
	if (requested || exiting) {
		requested = exiting = false;
		outcome = -ESHUTDOWN;
	}
	k_spin_unlock(&lock, key);
}

int drive_auto_exit_start(void)
{
	k_spinlock_key_t key = k_spin_lock(&lock);
	int rc = 0;
	struct k_sem *wake = worker_wake;
	if (requested || exiting) {
		rc = -EBUSY;
	} else if (!wake) {
		rc = -ENODEV;
	} else {
		requested = true;
		outcome = -EINPROGRESS;
		deadline_ms = k_uptime_get() + AUTO_EXIT_TIMEOUT_MS;
	}
	k_spin_unlock(&lock, key);
	if (!rc) {
		k_sem_give(wake);
	}
	return rc;
}

/* Called with lock held. A delayed request cannot reach the next worker. */
static void expire_request(void)
{
	if ((requested || exiting) && k_uptime_get() >= deadline_ms) {
		requested = exiting = false;
		outcome = -ETIMEDOUT;
	}
}

bool drive_auto_exit_take(void)
{
	k_spinlock_key_t key = k_spin_lock(&lock);
	expire_request();
	bool take = requested;
	if (take) {
		requested = false;
		exiting = true;
		worker_wake = NULL;
	}
	k_spin_unlock(&lock, key);
	return take;
}

void drive_auto_exit_confirm(void)
{
	k_spinlock_key_t key = k_spin_lock(&lock);
	worker_wake = NULL;
	if (requested || exiting) {
		outcome = exiting ? 0 : -ENODEV;
		requested = exiting = false;
	}
	k_spin_unlock(&lock, key);
}

int drive_auto_exit_status(void)
{
	k_spinlock_key_t key = k_spin_lock(&lock);
	expire_request();
	int result = outcome;
	k_spin_unlock(&lock, key);
	return result;
}
