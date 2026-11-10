/* Actual shared codec boundary. Files are synthetic inputs; no READY/authority/clock owner. */
#include "world.h"
#include "pb_decode.h"
#include <stdio.h>
#include <string.h>
_Static_assert(xenova_drive_OperationBinding_size<=128,"operation binding cap");
_Static_assert(xenova_drive_OperationStatus_size<=256,"operation status cap");
_Static_assert(xenova_drive_AutoInputPreflight_size<=768,"input preflight cap");
_Static_assert(xenova_drive_PiOperationGate_size<=96,"Pi operation gate cap");
_Static_assert(xenova_drive_MapperClockProbe_size<=1024,"original mapper probe cap");
_Static_assert(xenova_drive_MapperClockReply_size<=1024,"original mapper reply cap");
_Static_assert(xenova_drive_DriveLink_size<=32768,"original complete link cap");
_Static_assert(3+(xenova_drive_MissionExecution_size+4)+3*(xenova_drive_GearSegment_size+3)+
 (xenova_drive_DirectionGateRef_size+3)+(xenova_drive_TaskReferenceState_size+4)+
 (xenova_drive_EpisodeSnapshot_size+4)<=4096,"original State metadata cap");
static uint8_t wire[DW_WIRE_MAX];
static xenova_drive_DriveLink link;
static xenova_drive_PiOperationGate gate;
static xenova_drive_MapperClockProbe mapper_probe;
static xenova_drive_MapperClockReply mapper_reply;
int main(int argc,char **argv){
 if(argc!=3)return 2;
 FILE *f=fopen(argv[2],"rb");if(!f)return 2;
 size_t n=fread(wire,1,sizeof(wire),f);bool good=!ferror(f);if(fclose(f))good=false;if(!good)return 2;
 enum dw_result rc=DW_DECODE;pb_istream_t s=pb_istream_from_buffer(wire,n);
 if(!strcmp(argv[1],"link"))rc=dw_link_decode(wire,n,&link);
 else if(!strcmp(argv[1],"gate")&&pb_decode(&s,xenova_drive_PiOperationGate_fields,&gate))rc=dw_pi_operation_gate_validate(&gate);
 else if(!strcmp(argv[1],"mapper-probe")&&pb_decode(&s,xenova_drive_MapperClockProbe_fields,&mapper_probe))rc=dw_mapper_operation_probe_validate(&mapper_probe);
 else if(!strcmp(argv[1],"mapper-reply")&&pb_decode(&s,xenova_drive_MapperClockReply_fields,&mapper_reply))rc=dw_mapper_operation_reply_validate(&mapper_reply);
 puts(rc==DW_OK?"OK":"REJECT");return 0;
}
