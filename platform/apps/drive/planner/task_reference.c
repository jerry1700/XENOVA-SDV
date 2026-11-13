/* SPDX-License-Identifier: Apache-2.0 */
#include "task_reference.h"
#include "lattice.h"
#include <math.h>
#include <string.h>

static bool corridor_simple(const struct tr_view *);

static const xenova_drive_LaneGeometry *lane(const dw_world *w,uint32_t id){
 const xenova_drive_LaneGeometry *found=NULL;
 for(unsigned i=0;i<w->lane_model.lanes_count;i++)if(w->lane_model.lanes[i].lane_id==id){
  if(found)return NULL;
  found=&w->lane_model.lanes[i];
 }
 return found;
}
static bool lane_view(const dw_world *w,const struct tr_intent *intent,const struct lp_collision_index *index,struct tr_view *out){
 if(!w||!intent||!out||!intent->active||!(w->required_features&DW_LANE_MODEL)||!w->has_lane_model||
    w->lane_model.lanes_count!=2||w->lane_model.divider_count<2||w->lane_model.divider_count>32||
    !isfinite(intent->vehicle_pad)||intent->vehicle_pad<0)return false;
 const xenova_drive_LaneGeometry *to=lane(w,intent->selected_lane_id),*from=lane(w,intent->from_lane_id);
 unsigned n=w->lane_model.divider_count;
 if(!to||!from||!lane(w,intent->home_lane_id)||to->centerline_count!=n||from->centerline_count!=n||
    to->travel_direction!=1||from->travel_direction!=1||to->point_state.size!=n||from->point_state.size!=n||
    w->lane_model.crossing.size!=n-1||w->lane_model.divider_state.size!=n||
    (intent->changing?(to==from||to->adjacent_lane_id!=from->lane_id||from->adjacent_lane_id!=to->lane_id):to!=from))return false;
 /* 이 첫 실행 profile은 관측한 공동 station만 사용한다. 추정 끝을 경계 승인으로 쓰지 않는다. */
 if(!index||!index->lane_edges_ready)
  for(unsigned i=0;i<n;i++)if(to->point_state.bytes[i]||from->point_state.bytes[i]||w->lane_model.divider_state.bytes[i])return false;
 *out=(struct tr_view){w,to,from,to->centerline,n,index};return true;
}
bool tr_open_indexed(const dw_world *w,const struct tr_intent *intent,const struct lp_collision_index *index,struct tr_view *out){
 bool cached=w&&index&&index->world==w&&index->sequence==w->sequence;
 if(!lane_view(w,intent,cached?index:NULL,out))return false;
 return !intent->changing||(cached&&index->lane_edges_ready?index->lane_pair_simple:corridor_simple(out));
}
bool tr_open(const dw_world *w,const struct tr_intent *intent,struct tr_view *out){
 return tr_open_indexed(w,intent,NULL,out);
}
bool tr_centerline(const dw_world *w,const struct tr_intent *intent,const dw_point **points,unsigned *count){
 struct tr_view view;if(!lane_view(w,intent,NULL,&view))return false;
 *points=view.points;*count=view.count;return true;
}
static dw_point vertex(const struct tr_view *v,unsigned i){
 unsigned n=v->count;const xenova_drive_LaneModel *m=&v->world->lane_model;
 if(v->index&&v->index->lane_edges_ready){
  if(i<n)return v->index->lane_edges[v->selected-m->lanes].outer[i];
  unsigned j=2*n-1-i;
  return v->selected==v->from?m->divider[j]:v->index->lane_edges[v->from-m->lanes].outer[j];
 }
 if(i<n){dw_point c=v->selected->centerline[i],d=m->divider[i];return (dw_point){2*c.x-d.x,2*c.y-d.y};}
 unsigned j=2*n-1-i;dw_point d=m->divider[j];
 if(v->selected==v->from)return d;
 dw_point c=v->from->centerline[j];return (dw_point){2*c.x-d.x,2*c.y-d.y};
}
static dw_point tape_vertex(const struct tr_view *v,unsigned i,float tape){
 unsigned n=v->count,j=i<n?i:2*n-1-i;
 dw_point point=vertex(v,i);
 bool changing=v->selected!=v->from;
 /* A lane change may use both outer tapes within the observed corridor. */
 if(tape>0&&(!changing||(j>0&&j+1<n))){
  if(v->index&&v->index->lane_edges_ready&&v->index->lane_allowance==tape){
   const struct tr_lane_edges *edges=&v->index->lane_edges[(i<n?v->selected:v->from)-v->world->lane_model.lanes];
   return i<n||changing?edges->outer_tape[j]:edges->divider_tape[j];
  }
  dw_point a=(i<n?v->selected:v->from)->centerline[j],b=v->world->lane_model.divider[j];
  float x=a.x-b.x,y=a.y-b.y,length=hypotf(x,y);
  if(length>1e-6f){float scale=(i<n||changing?tape:-tape)/length;point.x+=scale*x;point.y+=scale*y;}
 }
 return point;
}
/* A footprint shares the same polygon across its axle or corner probes.
 * Keep the original crossing formula and vertex order at the boundary. */
static bool points_inside(const struct tr_view *v,const dw_point *points,unsigned count,float tape){
 unsigned hits=0,n=2*v->count;
 dw_point b=tape_vertex(v,n-1,tape);
 for(unsigned i=0;i<n;i++){
  dw_point a=tape_vertex(v,i,tape);
  for(unsigned k=0;k<count;k++){
   dw_point p=points[k];
   if(((a.y>p.y)!=(b.y>p.y))&&p.x<(b.x-a.x)*(p.y-a.y)/(b.y-a.y)+a.x)hits^=1u<<k;
  }
  b=a;
 }
 return hits==(1u<<count)-1;
}
static bool inside(const struct tr_view *v,dw_point p){return points_inside(v,&p,1,0);}
static bool cuts(dw_point a,dw_point b,dw_pose center,float cs,float sn,float half,float width){
 float ax=(a.x-center.x)*cs+(a.y-center.y)*sn,ay=-(a.x-center.x)*sn+(a.y-center.y)*cs;
 float bx=(b.x-center.x)*cs+(b.y-center.y)*sn,by=-(b.x-center.x)*sn+(b.y-center.y)*cs;
 if((ax>half&&bx>half)||(ax< -half&&bx< -half)||
    (ay>width&&by>width)||(ay< -width&&by< -width))return false;
 float lo=0,hi=1,first[2]={ax,ay},delta[2]={bx-ax,by-ay},bounds[2]={half,width};
 for(unsigned k=0;k<2;k++){
  if(fabsf(delta[k])<1e-9f){if(fabsf(first[k])>=bounds[k])return false;}
  else{float l=(-bounds[k]-first[k])/delta[k],h=(bounds[k]-first[k])/delta[k];
   if(l>h){float t=l;l=h;h=t;}lo=fmaxf(lo,l);hi=fminf(hi,h);if(lo>hi)return false;}
 }
 return lo<=hi;
}
bool tr_contains(const struct tr_view *v,dw_pose pose,float extra,const struct lp_config *c){
 if(!v||!c||!dw_pose_finite(pose)||!isfinite(extra)||extra<0)return false;
 const xenova_drive_LaneModel *m=&v->world->lane_model;
 float position=fmaxf(v->selected->position_bound_m,v->from->position_bound_m);
 float heading=fmaxf(v->selected->heading_bound_rad,v->from->heading_bound_rad);
 float radius=hypotf(fmaxf(c->front,c->rear)+c->margin,c->half_width+c->margin);
 /* outer=2*center-divider이므로 원 위치 오차도 같은 식으로 보존한다. */
 float error=2*position+m->divider_position_bound_m+radius*(2*heading+m->divider_heading_bound_rad);
 if(!isfinite(error)||error<0)return false;
 float half=(c->front+c->rear)*.5f+c->margin+extra+error,width=c->half_width+c->margin+extra+error;
 bool changing=v->selected!=v->from;
 float tape=lp_lane_boundary_allowance_m(c);
 float cs=cosf(pose.yaw),sn=sinf(pose.yaw);
 /* The body may use the boundary allowance. Axle centres stay inside the corridor. */
 if(tape>0){
  dw_point axles[2]={{pose.x,pose.y},{pose.x+c->wheelbase*cs,pose.y+c->wheelbase*sn}};
  if(!points_inside(v,axles,2,0))return false;
 }
 dw_pose center=pose;float shift=(c->front-c->rear)*.5f;
 center.x+=shift*cs;center.y+=shift*sn;
 dw_point corners[4];
 for(unsigned i=0;i<4;i++){
  float u=i&1?half:-half,z=i&2?width:-width;
  corners[i]=(dw_point){center.x+cs*u-sn*z,center.y+sn*u+cs*z};
 }
 if(!points_inside(v,corners,4,tape))return false;
 unsigned n=2*v->count;
 dw_point a=tape_vertex(v,0,tape);
 for(unsigned i=0;i<n;i++){
  dw_point b=tape_vertex(v,i+1==n?0:i+1,tape);
  if(cuts(a,b,center,cs,sn,half,width))return false;
  a=b;
 }
 for(unsigned i=0;(changing||tape<=0)&&i+1<v->count;i++){
  if(changing&&m->crossing.bytes[i]==1&&!m->divider_state.bytes[i]&&!m->divider_state.bytes[i+1])continue;
  if(cuts(m->divider[i],m->divider[i+1],center,cs,sn,half,width))return false;
 }
 return true;
}
uint32_t tr_unique_lane(const dw_world *w,dw_pose pose,float extra,const struct lp_config *c,const struct lp_collision_index *index){
 if(!w||!w->has_lane_model||w->lane_model.lanes_count!=2)return 0;
 uint32_t selected=0;
 for(unsigned i=0;i<2;i++){
  uint32_t id=w->lane_model.lanes[i].lane_id;struct tr_intent intent={true,false,id,id,id,0};struct tr_view view;
  if(tr_open_indexed(w,&intent,index,&view)&&tr_contains(&view,pose,extra,c)){if(selected)return 0;selected=id;}
 }
 return selected;
}
bool tr_same_intent(const struct tr_intent *a,const struct tr_intent *b){
 return a->active==b->active&&a->changing==b->changing&&a->selected_lane_id==b->selected_lane_id&&
        a->from_lane_id==b->from_lane_id&&a->home_lane_id==b->home_lane_id;
}
static float orient(dw_point a,dw_point b,dw_point c){return (b.x-a.x)*(c.y-a.y)-(b.y-a.y)*(c.x-a.x);}
static bool overlap(dw_point a,dw_point b,dw_point c,dw_point d){
 return fmaxf(fminf(a.x,b.x),fminf(c.x,d.x))<=fminf(fmaxf(a.x,b.x),fmaxf(c.x,d.x))&&
        fmaxf(fminf(a.y,b.y),fminf(c.y,d.y))<=fminf(fmaxf(a.y,b.y),fmaxf(c.y,d.y))&&
        orient(a,b,c)*orient(a,b,d)<=0&&orient(c,d,a)*orient(c,d,b)<=0;
}
static bool corridor_simple(const struct tr_view *v){
 unsigned count=2*v->count;
 for(unsigned i=0;i<count;i++){
  dw_point a=vertex(v,i),b=vertex(v,(i+1)%count);
  if(hypotf(a.x-b.x,a.y-b.y)<1e-6f)return false;
  for(unsigned j=i+2;j<count;j++){
   if(i==0&&j==count-1)continue;
   if(overlap(a,b,vertex(v,j),vertex(v,(j+1)%count)))return false;
  }
 }
 return true;
}
void tr_index_lanes(struct lp_collision_index *index,const dw_world *w,const struct lp_config *c){
 const xenova_drive_LaneModel *model=&w->lane_model;
 if(!w->has_lane_model||model->lanes_count!=2)return;
 struct tr_view views[2];
 for(unsigned i=0;i<2;i++){
  uint32_t id=model->lanes[i].lane_id;
  const struct tr_intent task={true,false,id,id,id,0};
  if(!lane_view(w,&task,NULL,&views[i])||views[i].selected!=&model->lanes[i])return;
 }
 index->lane_allowance=lp_lane_boundary_allowance_m(c);
 for(unsigned i=0;i<2;i++){
  const struct tr_view *view=&views[i];
  struct tr_lane_edges *edges=&index->lane_edges[i];
  edges->min_half_width=INFINITY;
  for(unsigned j=0;j<view->count;j++){
   dw_point center=view->points[j],divider=model->divider[j];
   edges->min_half_width=fminf(edges->min_half_width,hypotf(center.x-divider.x,center.y-divider.y));
   edges->outer[j]=vertex(view,j);
   edges->outer_tape[j]=tape_vertex(view,j,index->lane_allowance);
   edges->divider_tape[j]=tape_vertex(view,2*view->count-1-j,index->lane_allowance);
  }
 }
 index->lane_edges_ready=true;
 const struct tr_intent change={true,true,model->lanes[1].lane_id,model->lanes[0].lane_id,model->lanes[0].lane_id,0};
 struct tr_view view;
 if(lane_view(w,&change,index,&view))index->lane_pair_simple=corridor_simple(&view);
}
static bool open_basis(const dw_world *w){
 if(w->reference_count<2||w->reference_count>TR_BASIS_POINTS||!w->reference_revision||!dw_pose_finite(w->origin))return false;
 for(unsigned i=0;i<w->reference_count;i++){
  if(!isfinite(w->reference[i].x)||!isfinite(w->reference[i].y))return false;
  if(i&&hypotf(w->reference[i].x-w->reference[i-1].x,w->reference[i].y-w->reference[i-1].y)<1e-6f)return false;
  if(i>1){dw_point a=w->reference[i-2],b=w->reference[i-1],d=w->reference[i];
   if(fabsf(orient(a,b,d))<1e-6f&&((b.x-a.x)*(d.x-b.x)+(b.y-a.y)*(d.y-b.y))<0)return false;}
  for(unsigned j=1;j+1<i;j++)if(overlap(w->reference[j-1],w->reference[j],w->reference[i-1],w->reference[i]))return false;
 }
 return !(w->reference[0].x==w->reference[w->reference_count-1].x&&w->reference[0].y==w->reference[w->reference_count-1].y);
}
bool tr_basis_compatible(const struct tr_basis *basis,const dw_world *w){
 return basis&&w&&basis->bound&&open_basis(w)&&!strcmp(basis->id,w->reference_id)&&!strcmp(basis->map,w->map_epoch)&&
  !strcmp(basis->frame,w->frame_epoch)&&!strcmp(basis->odom,w->odom_epoch)&&!strcmp(basis->configuration,w->configuration)&&
  !strcmp(basis->calibration,w->calibration)&&!strcmp(basis->primitive,w->primitive)&&!strcmp(basis->vehicle,w->vehicle)&&
  !strcmp(basis->mapper_boot,w->mapper_boot)&&!strcmp(basis->mapper_instance,w->mapper_instance)&&!strcmp(basis->clock_epoch,w->clock_epoch)&&
  w->reference_count>=basis->count&&
  !memcmp(basis->points,w->reference,basis->count*sizeof(dw_point))&&
  (w->reference_count==basis->count?w->reference_revision==basis->revision:w->reference_revision>basis->revision);
}
bool tr_basis_accept(struct tr_basis *basis,const dw_world *w){
 if(!basis||!w||!open_basis(w)||(basis->bound&&!tr_basis_compatible(basis,w)))return false;
 basis->bound=true;basis->count=w->reference_count;basis->revision=w->reference_revision;
 memcpy(basis->points,w->reference,basis->count*sizeof(dw_point));strcpy(basis->id,w->reference_id);strcpy(basis->map,w->map_epoch);
 strcpy(basis->frame,w->frame_epoch);strcpy(basis->odom,w->odom_epoch);strcpy(basis->configuration,w->configuration);
 strcpy(basis->calibration,w->calibration);strcpy(basis->primitive,w->primitive);strcpy(basis->vehicle,w->vehicle);
 strcpy(basis->mapper_boot,w->mapper_boot);strcpy(basis->mapper_instance,w->mapper_instance);strcpy(basis->clock_epoch,w->clock_epoch);return true;
}
bool tr_parking_approach(const struct tr_view *v,uint32_t id){
 if(!v||!id||v->selected!=v->from||!(v->world->required_features&DW_PARKING_MODEL))return false;
 for(unsigned i=0;i<v->world->parking_slots_count;i++){
  const xenova_drive_ParkingSlot *s=&v->world->parking_slots[i];
  if(s->slot_id==id)return s->approach_lane_id==v->selected->lane_id&&s->geometry_complete&&s->geometry_state==1;
 }
 return false;
}
static bool in_slot(const dw_point p[4],dw_point q){
 float sign=0;
 for(unsigned k=0;k<4;k++){
  float cross=orient(p[k],p[(k+1)%4],q);
  if(fabsf(cross)<1e-7f)continue;
  if(sign&&sign*cross<0)return false;
  sign=cross;
 }
 return sign!=0;
}
static bool body_inside(dw_pose center,float half,float width,dw_point p){
 float dx=p.x-center.x,dy=p.y-center.y;
 return fabsf(dx*cosf(center.yaw)+dy*sinf(center.yaw))<half-1e-6f&&
        fabsf(-dx*sinf(center.yaw)+dy*cosf(center.yaw))<width-1e-6f;
}
/* Split at intersections before classifying the middle of each portion. */
static unsigned edge_cuts(const struct tr_view *v,const dw_point slot[4],dw_point a,dw_point b,
 bool road_edge,float t[132]){
 t[0]=0;t[1]=1;
 unsigned count=2,n=road_edge?4:2*v->count;
 for(unsigned k=0;k<n;k++){
  dw_point c=road_edge?slot[k]:vertex(v,k),d=road_edge?slot[(k+1)%4]:vertex(v,(k+1)%n);
  float ax=b.x-a.x,ay=b.y-a.y,bx=d.x-c.x,by=d.y-c.y,den=ax*by-ay*bx;
  if(fabsf(den)<1e-9f)continue;
  float u=((c.x-a.x)*by-(c.y-a.y)*bx)/den,z=((c.x-a.x)*ay-(c.y-a.y)*ax)/den;
  if(u>0&&u<1&&z>=0&&z<=1)t[count++]=u;
 }
 for(unsigned k=1;k<count;k++){float value=t[k];unsigned j=k;while(j&&t[j-1]>value){t[j]=t[j-1];j--;}t[j]=value;}
 return count;
}
static bool union_edge(const struct tr_view *v,const dw_point slot[4],dw_point a,dw_point b,
 bool road_edge,dw_pose center,float half,float width,struct tr_parking_edges *edges){
 float t[132];unsigned count=edge_cuts(v,slot,a,b,road_edge,t);
 for(unsigned k=1;k<count;k++){
  float at=.5f*(t[k-1]+t[k]);dw_point q={a.x+(b.x-a.x)*at,a.y+(b.y-a.y)*at};
  if((edges||body_inside(center,half,width,q))&&!(road_edge?in_slot(slot,q):inside(v,q))){
   if(!edges||edges->count>=TR_PARKING_EDGE_SAMPLES)return false;
   edges->points[edges->count++]=q;
  }
 }
 return true;
}
static bool union_edges(const struct tr_view *v,const dw_point slot[4],dw_pose center,float half,float width,
 struct tr_parking_edges *edges){
 unsigned n=2*v->count;
 for(unsigned k=0;k<n;k++)if(!union_edge(v,slot,vertex(v,k),vertex(v,(k+1)%n),true,center,half,width,edges))return false;
 for(unsigned k=0;k<4;k++)if(!union_edge(v,slot,slot[k],slot[(k+1)%4],false,center,half,width,edges))return false;
 return true;
}
static bool parking_contains(const struct tr_view *v,const dw_point slot[4],float slot_pad,
 dw_pose pose,float extra,const struct lp_config *c,const struct tr_parking_edges *edges){
 const xenova_drive_LaneModel *m=&v->world->lane_model;
 float position=fmaxf(v->selected->position_bound_m,v->from->position_bound_m);
 float heading=fmaxf(v->selected->heading_bound_rad,v->from->heading_bound_rad);
 float radius=hypotf(fmaxf(c->front,c->rear)+c->margin,c->half_width+c->margin);
 float error=fmaxf(slot_pad,2*position+m->divider_position_bound_m+
  radius*(2*heading+m->divider_heading_bound_rad));
 float half=.5f*(c->front+c->rear)+c->margin+extra+error,width=c->half_width+c->margin+extra+error;
 dw_pose center=pose;center.x+=.5f*(c->front-c->rear)*cosf(pose.yaw);center.y+=.5f*(c->front-c->rear)*sinf(pose.yaw);
 for(unsigned k=0;k<4;k++){
  float x=k&1?half:-half,y=k&2?width:-width;
  dw_point p={center.x+cosf(pose.yaw)*x-sinf(pose.yaw)*y,center.y+sinf(pose.yaw)*x+cosf(pose.yaw)*y};
  if(!inside(v,p)&&!in_slot(slot,p))return false;
 }
 if(edges&&edges->ready){
  for(unsigned k=0;k<edges->count;k++)if(body_inside(center,half,width,edges->points[k]))return false;
 }else if(!union_edges(v,slot,center,half,width,NULL))return false;
 if(v->selected!=v->from)for(unsigned k=0;k+1<v->count;k++){
  if(m->crossing.bytes[k]!=1&&cuts(m->divider[k],m->divider[k+1],center,cosf(center.yaw),sinf(center.yaw),half,width))return false;
 }
 return true;
}
static bool parking_both(const struct tr_view *v,struct tr_view *both){
 uint32_t aisle=v->selected->lane_id;
 struct tr_intent intent={true,true,aisle,v->selected->adjacent_lane_id,aisle,0};
 return tr_open_indexed(v->world,&intent,v->index,both);
}
bool tr_parking_contains(const struct tr_view *v,const dw_point slot[4],float slot_pad,
 dw_pose pose,float extra,const struct lp_config *c){
 if(!v||!slot||!c||v->selected!=v->from||!dw_pose_finite(pose)||!isfinite(extra)||extra<0||
    !isfinite(slot_pad)||slot_pad<0)return false;
 if(parking_contains(v,slot,slot_pad,pose,extra,c,NULL))return true;
 /* A marked crossing in the current observed lane pair can supply room for
  * parking. The same whole-cell and occupancy checks still cover each sweep. */
 struct tr_view both;
 return parking_both(v,&both)&&parking_contains(&both,slot,slot_pad,pose,extra,c,NULL);
}
void tr_prepare_parking(struct tr_parking_geometry *geometry,const struct tr_view *v,const dw_point slot[4]){
 geometry->aisle=*v;
 geometry->aisle_edges.count=0;
 geometry->aisle_edges.ready=union_edges(v,slot,(dw_pose){0},0,0,&geometry->aisle_edges);
 geometry->has_both=parking_both(v,&geometry->both);
 geometry->both_edges.count=0;
 geometry->both_edges.ready=geometry->has_both&&
  union_edges(&geometry->both,slot,(dw_pose){0},0,0,&geometry->both_edges);
}
bool tr_prepared_parking_contains(const struct tr_parking_geometry *geometry,const dw_point slot[4],float slot_pad,
 dw_pose pose,float extra,const struct lp_config *c){
 if(!dw_pose_finite(pose)||!isfinite(extra)||extra<0||!isfinite(slot_pad)||slot_pad<0)return false;
 return parking_contains(&geometry->aisle,slot,slot_pad,pose,extra,c,&geometry->aisle_edges)||
  (geometry->has_both&&parking_contains(&geometry->both,slot,slot_pad,pose,extra,c,&geometry->both_edges));
}
