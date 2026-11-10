/* Actual planner supervision helper; the tracker never writes this evidence. */
#include "planner_health.h"
#include <assert.h>
#include <stdio.h>

int main(void){
 struct drive_planner_health h={0};
 assert(!drive_planner_health_current(&h,0));
 drive_planner_health_complete(&h,1000000);
 assert(drive_planner_health_current(&h,1299999));
 assert(!drive_planner_health_current(&h,1300000));

 /* A normal 2s search may exceed tracker readiness by far. BUSY slices cannot
  * replace its original deadline with each new outer callback's time. */
 drive_planner_health_begin(&h,4000000);
 drive_planner_health_admitted(&h,4000000);
 assert(drive_planner_health_current(&h,3500000));
 for(uint64_t now=3500000;now<5000000;now+=20000){
  drive_planner_health_begin(&h,now+2000000);
  assert(drive_planner_health_current(&h,now)==(now<4300000));
 }
 /* A newly admitted apply owns its exact engine.apply_budget, independently
  * of the earlier BUSY attempt. Later finish polls cannot renew that budget. */
 drive_planner_health_admitted(&h,6000000);
 assert(drive_planner_health_current(&h,5500000));
 drive_planner_health_begin(&h,9000000);
 assert(drive_planner_health_current(&h,6000000));
 assert(!drive_planner_health_current(&h,6300000));
 /* The health observation window cannot rewrite a search/apply budget or
  * be renewed by retries. An absent original deadline supplies no evidence. */
 drive_planner_health_admitted(&h,0);
 assert(!drive_planner_health_current(&h,1));
 /* No-path is a completed planner calculation, not a dead owner. New real
  * negative completions may keep planning available without producing DRIVE. */
 for(uint64_t now=7000000;now<8000000;now+=100000){
  drive_planner_health_complete(&h,now);
  assert(drive_planner_health_current(&h,now+250000));
 }
 /* Stop the planner owner while external tracker callbacks keep running. */
 for(uint64_t tracker=8000000;tracker<9000000;tracker+=20000){
  assert(drive_planner_health_current(&h,tracker)==(tracker<8200000));
 }
 puts("planner-health: original search/apply, BUSY, no-path, stopped-owner PASS");
 return 0;
}
