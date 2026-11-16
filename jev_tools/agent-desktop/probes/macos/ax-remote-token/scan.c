#include <ApplicationServices/ApplicationServices.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
extern CFDataRef _AXUIElementRemoteTokenCreate(AXUIElementRef);
extern AXUIElementRef _AXUIElementCreateWithRemoteToken(CFDataRef);
static void str(AXUIElementRef e, CFStringRef a, char *out, size_t n){ out[0]=0; CFTypeRef v=NULL; if(AXUIElementCopyAttributeValue(e,a,&v)==kAXErrorSuccess && v){ if(CFGetTypeID(v)==CFStringGetTypeID()) CFStringGetCString(v,out,n,kCFStringEncodingUTF8); CFRelease(v);} }
static void label(AXUIElementRef e, char *out, size_t n){ char t[200],v[200],d[200]; str(e,kAXTitleAttribute,t,200); str(e,kAXValueAttribute,v,200); str(e,kAXDescriptionAttribute,d,200); snprintf(out,n,"%s/%s/%s",t,v,d); for(char*p=out;*p;p++) if(*p=='|'||*p=='\n') *p=' '; }
static int count=0;
static void walk(AXUIElementRef e,int depth,int max){
  if(depth>max||count>4000) return; char role[64]; str(e,kAXRoleAttribute,role,64);
  if(!strcmp(role,"AXRow")||!strcmp(role,"AXCell")||!strcmp(role,"AXStaticText")||!strcmp(role,"AXButton")||!strcmp(role,"AXTextField")){
    char l[600]; label(e,l,600); CFDataRef t=_AXUIElementRemoteTokenCreate(e); printf("%s|%s|",role,l); if(t){const UInt8*b=CFDataGetBytePtr(t); for(CFIndex i=0;i<CFDataGetLength(t);i++) printf("%02x",b[i]); CFRelease(t);} printf("\n"); count++; }
  CFArrayRef kids=NULL; if(AXUIElementCopyAttributeValue(e,kAXChildrenAttribute,(CFTypeRef*)&kids)==kAXErrorSuccess&&kids){ for(CFIndex i=0;i<CFArrayGetCount(kids);i++) walk((AXUIElementRef)CFArrayGetValueAtIndex(kids,i),depth+1,max); CFRelease(kids);} }
int main(int argc,char**argv){
  if(!strcmp(argv[1],"dump")){ walk(AXUIElementCreateApplication(atoi(argv[2])),0,60); return 0; }
  if(!strcmp(argv[1],"check")){ FILE*f=fopen(argv[2],"r"); char line[2000]; int same=0,dead=0,changed=0; while(fgets(line,sizeof line,f)){ char*role=strtok(line,"|"); char*lab=strtok(NULL,"|"); char*tok=strtok(NULL,"|\n"); if(!tok||!lab) continue; size_t n=strlen(tok)/2; UInt8*buf=malloc(n); for(size_t i=0;i<n;i++) sscanf(tok+2*i,"%2hhx",&buf[i]); CFDataRef d=CFDataCreate(NULL,buf,n); AXUIElementRef e=_AXUIElementCreateWithRemoteToken(d); CFTypeRef v=NULL; AXError err=e?AXUIElementCopyAttributeValue(e,kAXRoleAttribute,&v):-1; if(v)CFRelease(v); if(err!=kAXErrorSuccess){dead++; continue;} char r2[64],l2[600]; str(e,kAXRoleAttribute,r2,64); label(e,l2,600); if(strcmp(r2,role)||strcmp(l2,lab)){changed++; printf("CHANGED %s [%s] -> %s [%s]\n",role,lab,r2,l2);} else same++; free(buf);} printf("same=%d dead=%d changed=%d\n",same,dead,changed); return 0; }
  return 2; }
