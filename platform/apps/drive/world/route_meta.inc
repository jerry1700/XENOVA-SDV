/* route schema bounds/byte IDs; structure only, no current authority. */
static size_t route_exact_bytes(const pb_msgdesc_t *desc,unsigned field){
 if(desc==xenova_drive_RouteBinding_fields){switch(field){case 1:return 16;case 4:return 32;case 5:return 32;case 6:return 32;default:return 0;}}
 if(desc==xenova_drive_StartGoal_fields){switch(field){case 2:return 16;case 4:return 32;case 8:return 16;case 12:return 32;case 13:return 32;case 14:return 32;case 15:return 32;case 20:return 32;default:return 0;}}
 if(desc==xenova_drive_RoadSectionRef_fields){switch(field){case 1:return 16;case 2:return 32;default:return 0;}}
 if(desc==xenova_drive_RoadEdge_fields){switch(field){case 8:return 32;default:return 0;}}
 if(desc==xenova_drive_RoadTopology_fields){switch(field){case 2:return 16;case 4:return 32;case 8:return 32;default:return 0;}}
 if(desc==xenova_drive_RoadSection_fields){switch(field){case 2:return 16;case 4:return 32;case 12:return 32;case 13:return 32;default:return 0;}}
 if(desc==xenova_drive_RoadConnection_fields){switch(field){case 2:return 16;case 4:return 32;case 6:return 32;case 7:return 32;case 17:return 32;default:return 0;}}
 if(desc==xenova_drive_ExitSpace_fields){switch(field){case 2:return 16;case 4:return 32;case 6:return 16;case 7:return 32;case 12:return 32;case 13:return 32;default:return 0;}}
 if(desc==xenova_drive_RoutePlan_fields){switch(field){case 2:return 16;case 5:return 32;case 6:return 32;case 7:return 32;case 10:return 32;case 11:return 32;case 12:return 16;case 17:return 32;default:return 0;}}
 if(desc==xenova_drive_RouteCore_fields){switch(field){case 1:return 16;case 4:return 32;case 5:return 32;case 6:return 32;case 7:return 32;case 10:return 16;case 17:return 16;case 18:return 32;case 19:return 32;case 20:return 32;case 24:return 32;case 29:return 32;default:return 0;}}
 if(desc==xenova_drive_RouteAcceptanceRef_fields){switch(field){case 1:return 16;case 3:return 16;case 4:return 16;case 5:return 32;case 6:return 32;case 8:return 32;default:return 0;}}
 if(desc==xenova_drive_RouteSnapshot_fields){switch(field){case 4:return 32;case 7:return 32;case 8:return 32;case 10:return 32;default:return 0;}}
 if(desc==xenova_drive_WorldRouteView_fields){switch(field){case 2:return 32;case 3:return 32;case 4:return 16;case 6:return 32;case 7:return 16;case 8:return 32;case 9:return 16;case 10:return 16;case 11:return 16;case 12:return 32;default:return 0;}}
 if(desc==xenova_drive_RouteStatus_fields){switch(field){case 2:return 16;case 4:return 16;case 5:return 32;case 6:return 32;case 7:return 32;case 12:return 16;case 13:return 32;case 19:return 32;case 20:return 32;default:return 0;}}
 if(desc==xenova_drive_RouteQuery_fields){switch(field){case 2:return 16;case 4:return 32;case 8:return 16;default:return 0;}}
 if(desc==xenova_drive_RouteCandidate_fields){switch(field){case 2:return 16;case 5:return 16;case 6:return 32;case 9:return 16;case 15:return 32;case 16:return 32;case 18:return 32;case 19:return 16;case 24:return 32;default:return 0;}}
 if(desc==xenova_drive_RouteReceipt_fields){switch(field){case 2:return 16;case 3:return 32;case 8:return 16;case 9:return 32;default:return 0;}}
 if(desc==xenova_drive_RoadWindow_fields){switch(field){case 2:return 16;case 4:return 32;case 5:return 32;case 6:return 32;case 14:return 16;case 15:return 32;case 16:return 32;case 17:return 16;default:return 0;}}
 if(desc==xenova_drive_RoadGeometryScope_fields){switch(field){case 2:return 16;case 3:return 16;case 4:return 16;case 8:return 32;case 9:return 16;case 10:return 16;case 11:return 16;case 12:return 16;case 13:return 16;case 14:return 32;case 15:return 32;case 17:return 32;default:return 0;}}
 if(desc==xenova_drive_RoadDurableRef_fields){switch(field){case 1:return 16;case 2:return 16;case 3:return 32;case 7:return 32;case 8:return 32;case 9:return 32;default:return 0;}}
 if(desc==xenova_drive_RoadObservationOrigin_fields){switch(field){case 2:return 16;case 3:return 16;case 4:return 16;case 10:return 32;case 11:return 32;case 12:return 32;case 13:return 32;case 14:return 32;case 16:return 32;default:return 0;}}
 if(desc==xenova_drive_MapperRouteWitness_fields){switch(field){case 1:return 16;case 2:return 16;case 3:return 16;case 4:return 16;case 5:return 16;case 7:return 32;case 11:return 32;case 14:return 32;case 15:return 16;default:return 0;}}
 if(desc==xenova_drive_RouteOwnerContext_fields){switch(field){case 1:return 16;case 2:return 16;case 3:return 16;case 6:return 16;case 10:return 16;case 12:return 32;case 13:return 32;default:return 0;}}
 if(desc==xenova_drive_RouteCurrentWitness_fields){switch(field){case 6:return 32;case 7:return 32;case 8:return 32;default:return 0;}}
 if(desc==xenova_drive_RouteExtentProof_fields){switch(field){case 2:return 32;case 3:return 32;case 12:return 32;case 13:return 32;default:return 0;}}
 if(desc==xenova_drive_RouteEligibility_fields){switch(field){case 2:return 16;case 7:return 32;case 8:return 32;case 9:return 32;case 10:return 32;case 11:return 32;default:return 0;}}
 if(desc==xenova_drive_RouteEligibilityRef_fields){switch(field){case 1:return 16;case 2:return 32;default:return 0;}}
 if(desc==xenova_drive_MapperWorldWitness_fields){switch(field){case 4:return 32;default:return 0;}}
 if(desc==xenova_drive_WindowTransitionRequest_fields){switch(field){case 2:return 16;case 5:return 16;case 6:return 32;case 7:return 16;case 8:return 16;case 10:return 16;case 11:return 32;case 12:return 32;case 13:return 32;case 14:return 32;default:return 0;}}
 if(desc==xenova_drive_WindowTransitionReceipt_fields){switch(field){case 2:return 16;case 3:return 32;case 4:return 16;case 6:return 32;case 7:return 32;case 8:return 32;case 10:return 32;default:return 0;}}
 if(desc==xenova_drive_RoadDataRequest_fields){switch(field){case 2:return 16;case 4:return 32;default:return 0;}}
 if(desc==xenova_drive_RoadRegistrationRequest_fields){switch(field){case 2:return 16;case 5:return 32;case 6:return 32;default:return 0;}}
 if(desc==xenova_drive_RoadData_fields){switch(field){case 6:return 32;default:return 0;}}
 return 0;
}
static size_t route_float_size(const pb_msgdesc_t *desc,unsigned field){
 if(desc==xenova_drive_StartGoal_fields){switch(field){case 6:return 4;case 7:return 4;case 10:return 4;default:return 0;}}
 if(desc==xenova_drive_RoadSectionRef_fields){switch(field){case 5:return 4;default:return 0;}}
 if(desc==xenova_drive_RoadEdge_fields){switch(field){case 6:return 4;case 7:return 4;default:return 0;}}
 if(desc==xenova_drive_RoadSection_fields){switch(field){case 10:return 4;case 11:return 4;default:return 0;}}
 if(desc==xenova_drive_RoadConnection_fields){switch(field){case 13:return 4;case 15:return 4;default:return 0;}}
 if(desc==xenova_drive_ExitSpace_fields){switch(field){case 14:return 4;case 15:return 4;default:return 0;}}
 if(desc==xenova_drive_RouteOccurrence_fields){switch(field){case 3:return 4;case 4:return 4;default:return 0;}}
 if(desc==xenova_drive_RoutePlan_fields){switch(field){case 14:return 4;case 15:return 4;default:return 0;}}
 if(desc==xenova_drive_RouteCore_fields){switch(field){case 12:return 8;case 26:return 8;default:return 0;}}
 if(desc==xenova_drive_RouteProgress_fields){switch(field){case 2:return 4;case 3:return 4;case 11:return 4;default:return 0;}}
 if(desc==xenova_drive_RoadWindow_fields){switch(field){case 12:return 4;case 13:return 4;default:return 0;}}
 if(desc==xenova_drive_RouteExtentProof_fields){switch(field){case 4:return 4;case 5:return 4;case 6:return 4;case 7:return 4;case 8:return 4;case 9:return 4;case 10:return 4;case 11:return 4;default:return 0;}}
 return 0;
}
static size_t route_cap(const pb_msgdesc_t *desc){
 if(desc==xenova_drive_RouteBinding_fields)return 160u;
 if(desc==xenova_drive_StartGoal_fields)return 1024u;
 if(desc==xenova_drive_RoadSectionRef_fields)return 73u;
 if(desc==xenova_drive_RoadEdge_fields)return 74u;
 if(desc==xenova_drive_RoadTopology_fields)return 12288u;
 if(desc==xenova_drive_RoadSection_fields)return 4096u;
 if(desc==xenova_drive_RoadConnection_fields)return 1536u;
 if(desc==xenova_drive_ExitSpace_fields)return 1024u;
 if(desc==xenova_drive_RouteOccurrence_fields)return 27u;
 if(desc==xenova_drive_RoutePlan_fields)return 4096u;
 if(desc==xenova_drive_RouteCore_fields)return 768u;
 if(desc==xenova_drive_RouteProgress_fields)return 128u;
 if(desc==xenova_drive_RouteAcceptanceRef_fields)return 256u;
 if(desc==xenova_drive_RouteSnapshot_fields)return 1024u;
 if(desc==xenova_drive_WorldRouteView_fields)return 384u;
 if(desc==xenova_drive_RouteStatus_fields)return 768u;
 if(desc==xenova_drive_RouteQuery_fields)return 377u;
 if(desc==xenova_drive_RouteCandidate_fields)return 2816u;
 if(desc==xenova_drive_RouteReceipt_fields)return 1536u;
 if(desc==xenova_drive_RoadWindow_fields)return 4096u;
 if(desc==xenova_drive_RoadGeometryScope_fields)return 768u;
 if(desc==xenova_drive_RoadDurableRef_fields)return 256u;
 if(desc==xenova_drive_RoadObservationOrigin_fields)return 512u;
 if(desc==xenova_drive_MapperRouteWitness_fields)return 512u;
 if(desc==xenova_drive_RouteOwnerContext_fields)return 320u;
 if(desc==xenova_drive_RouteCurrentWitness_fields)return 768u;
 if(desc==xenova_drive_RouteExtentProof_fields)return 320u;
 if(desc==xenova_drive_RouteEligibility_fields)return 2048u;
 if(desc==xenova_drive_RouteEligibilityRef_fields)return 94u;
 if(desc==xenova_drive_MapperWorldWitness_fields)return 1024u;
 if(desc==xenova_drive_WindowTransitionRequest_fields)return 2048u;
 if(desc==xenova_drive_WindowTransitionReceipt_fields)return 1024u;
 if(desc==xenova_drive_RoadDataRequest_fields)return 348u;
 if(desc==xenova_drive_RoadRegistrationRequest_fields)return 2048u;
 if(desc==xenova_drive_RoadData_fields)return 16384u;
 if(desc==xenova_drive_RouteExchange_fields)return 3072u;
 return 0;
}
static bool route_has_major(const pb_msgdesc_t *desc){
 if(desc==xenova_drive_StartGoal_fields)return true;
 if(desc==xenova_drive_RoadTopology_fields)return true;
 if(desc==xenova_drive_RoadSection_fields)return true;
 if(desc==xenova_drive_RoadConnection_fields)return true;
 if(desc==xenova_drive_ExitSpace_fields)return true;
 if(desc==xenova_drive_RoutePlan_fields)return true;
 if(desc==xenova_drive_WorldRouteView_fields)return true;
 if(desc==xenova_drive_RouteStatus_fields)return true;
 if(desc==xenova_drive_RouteQuery_fields)return true;
 if(desc==xenova_drive_RouteCandidate_fields)return true;
 if(desc==xenova_drive_RouteReceipt_fields)return true;
 if(desc==xenova_drive_RoadWindow_fields)return true;
 if(desc==xenova_drive_RoadGeometryScope_fields)return true;
 if(desc==xenova_drive_RoadObservationOrigin_fields)return true;
 if(desc==xenova_drive_RouteEligibility_fields)return true;
 if(desc==xenova_drive_MapperWorldWitness_fields)return true;
 if(desc==xenova_drive_WindowTransitionRequest_fields)return true;
 if(desc==xenova_drive_WindowTransitionReceipt_fields)return true;
 if(desc==xenova_drive_RoadDataRequest_fields)return true;
 if(desc==xenova_drive_RoadRegistrationRequest_fields)return true;
 if(desc==xenova_drive_RoadData_fields)return true;
 if(desc==xenova_drive_RouteExchange_fields)return true;
 return false;
}
