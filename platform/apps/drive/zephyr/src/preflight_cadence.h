/* Internal scheduling bounds; neither a wire profile nor an input lease.
 * A Pi/guest original support window is at most1s and its owner keeps32 raw
 * replies per source. Limit A1 report scheduling to25 probes/s; legacy Episode
 * urgency stays independent and cannot be caused by a report notification.
 * Report expiry remains the original100ms/current-input minimum. */
#ifndef XENOVA_PREFLIGHT_CADENCE_H
#define XENOVA_PREFLIGHT_CADENCE_H
#include <stdbool.h>
#include <stdint.h>
#define XPF_REPORT_US UINT64_C(100000)
#define XPF_PROBE_MIN_US UINT64_C(40000)
static inline bool xpf_elapsed(uint64_t now,uint64_t last){
 return !last||(now>=last&&now-last>=XPF_PROBE_MIN_US);
}
static inline uint64_t xpf_refresh_lead(uint64_t clock_width){
 uint64_t maximum=XPF_REPORT_US-XPF_PROBE_MIN_US;
 return XPF_PROBE_MIN_US+(clock_width<maximum?clock_width:maximum);
}
#endif
