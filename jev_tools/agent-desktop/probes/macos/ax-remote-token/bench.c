#include <ApplicationServices/ApplicationServices.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <mach/mach_time.h>
extern CFDataRef _AXUIElementRemoteTokenCreate(AXUIElementRef);
extern AXUIElementRef _AXUIElementCreateWithRemoteToken(CFDataRef);
static double ms(uint64_t a,uint64_t b){ mach_timebase_info_data_t t; mach_timebase_info(&t); return (double)(b-a)*t.numer/t.denom/1e6; }
static void str(AXUIElementRef e, CFStringRef a, char *out, size_t n){ out[0]=0; CFTypeRef v=NULL; if(AXUIElementCopyAttributeValue(e,a,&v)==kAXErrorSuccess && v){ if(CFGetTypeID(v)==CFStringGetTypeID()) CFStringGetCString(v,out,n,kCFStringEncodingUTF8); CFRelease(v);} }
static AXUIElementRef found=NULL; static int visited=0;
static void find(AXUIElementRef e,const char*want,int d){ if(found||d>60) return; visited++; char v[300]; str(e,kAXValueAttribute,v,300); if(!strcmp(v,want)){CFRetain(e); found=e; return;} CFArrayRef k=NULL; if(AXUIElementCopyAttributeValue(e,kAXChildrenAttribute,(CFTypeRef*)&k)==kAXErrorSuccess&&k){ for(CFIndex i=0;i<CFArrayGetCount(k)&&!found;i++) find((AXUIElementRef)CFArrayGetValueAtIndex(k,i),want,d+1); CFRelease(k);} }
int main(int argc,char**argv){
  pid_t pid=atoi(argv[1]); AXUIElementRef app=AXUIElementCreateApplication(pid);
  uint64_t a=mach_absolute_time(); find(app,argv[2],0); uint64_t b=mach_absolute_time();
  if(!found){printf("target not found\n");return 1;}
  printf("tree walk to '%s': %.1f ms, %d elements visited\n",argv[2],ms(a,b),visited);
  CFDataRef tok=_AXUIElementRemoteTokenCreate(found);
  double best=1e9,total=0; for(int i=0;i<50;i++){ uint64_t c=mach_absolute_time(); AXUIElementRef e=_AXUIElementCreateWithRemoteToken(tok); CFTypeRef r=NULL; AXUIElementCopyAttributeValue(e,kAXRoleAttribute,&r); if(r)CFRelease(r); CFRelease(e); double t=ms(c,mach_absolute_time()); total+=t; if(t<best)best=t; }
  printf("token resolve + role read: min %.3f ms, avg %.3f ms (50 runs)\n",best,total/50);
  return 0; }
