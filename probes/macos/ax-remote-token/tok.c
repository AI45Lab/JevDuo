#include <ApplicationServices/ApplicationServices.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
extern CFDataRef _AXUIElementRemoteTokenCreate(AXUIElementRef);
extern AXUIElementRef _AXUIElementCreateWithRemoteToken(CFDataRef);
static void str(AXUIElementRef e, CFStringRef a, char *out, size_t n){ out[0]=0; CFTypeRef v=NULL; if(AXUIElementCopyAttributeValue(e,a,&v)==kAXErrorSuccess && v){ if(CFGetTypeID(v)==CFStringGetTypeID()) CFStringGetCString(v,out,n,kCFStringEncodingUTF8); CFRelease(v);} }
static void hex(CFDataRef d){ const UInt8*b=CFDataGetBytePtr(d); for(CFIndex i=0;i<CFDataGetLength(d);i++) printf("%02x",b[i]); }
static void walk(AXUIElementRef e, const char *want, int depth){
  if(depth>40) return;
  char id[256]; str(e,kAXIdentifierAttribute,id,sizeof id);
  if((want[0]=='*' || strcmp(id,want)==0) && id[0]){ char role[64],val[256]; str(e,kAXRoleAttribute,role,sizeof role); str(e,kAXValueAttribute,val,sizeof val); CFDataRef t=_AXUIElementRemoteTokenCreate(e); printf("%s|%s|%s|",id,role,val); if(t){hex(t); CFRelease(t);} else printf("NOTOKEN"); printf("\n"); }
  CFArrayRef kids=NULL; if(AXUIElementCopyAttributeValue(e,kAXChildrenAttribute,(CFTypeRef*)&kids)==kAXErrorSuccess && kids){ for(CFIndex i=0;i<CFArrayGetCount(kids);i++) walk((AXUIElementRef)CFArrayGetValueAtIndex(kids,i),want,depth+1); CFRelease(kids);} }
static AXUIElementRef find(AXUIElementRef e, const char *want, int depth){
  if(depth>40) return NULL; char id[256]; str(e,kAXIdentifierAttribute,id,sizeof id); if(strcmp(id,want)==0){CFRetain(e); return e;}
  CFArrayRef kids=NULL; AXUIElementRef r=NULL; if(AXUIElementCopyAttributeValue(e,kAXChildrenAttribute,(CFTypeRef*)&kids)==kAXErrorSuccess && kids){ for(CFIndex i=0;i<CFArrayGetCount(kids)&&!r;i++) r=find((AXUIElementRef)CFArrayGetValueAtIndex(kids,i),want,depth+1); CFRelease(kids);} return r; }
int main(int argc,char**argv){
  if(argc<3){fprintf(stderr,"usage\n");return 2;}
  if(!strcmp(argv[1],"dump")){ AXUIElementRef app=AXUIElementCreateApplication(atoi(argv[2])); walk(app,argc>3?argv[3]:"*",0); return 0; }
  if(!strcmp(argv[1],"press")){ AXUIElementRef app=AXUIElementCreateApplication(atoi(argv[2])); AXUIElementRef b=find(app,argv[3],0); if(!b){printf("press: not found\n");return 1;} printf("press: %d\n",AXUIElementPerformAction(b,kAXPressAction)); return 0; }
  if(!strcmp(argv[1],"resolve")){ size_t n=strlen(argv[2])/2; UInt8*buf=malloc(n); for(size_t i=0;i<n;i++) sscanf(argv[2]+2*i,"%2hhx",&buf[i]); CFDataRef d=CFDataCreate(NULL,buf,n); AXUIElementRef e=_AXUIElementCreateWithRemoteToken(d); if(!e){printf("resolve: NULL element\n");return 1;} char role[64],id[256],val[256]; CFTypeRef v=NULL; AXError err=AXUIElementCopyAttributeValue(e,kAXRoleAttribute,&v); if(v)CFRelease(v); str(e,kAXRoleAttribute,role,sizeof role); str(e,kAXIdentifierAttribute,id,sizeof id); str(e,kAXValueAttribute,val,sizeof val); pid_t p=0; AXUIElementGetPid(e,&p); printf("resolve: err=%d pid=%d role=%s id=%s value=%s\n",err,p,role,id,val); return 0; }
  return 2; }
