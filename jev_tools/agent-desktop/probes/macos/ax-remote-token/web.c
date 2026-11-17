#include <ApplicationServices/ApplicationServices.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
extern CFDataRef _AXUIElementRemoteTokenCreate(AXUIElementRef);
extern AXUIElementRef _AXUIElementCreateWithRemoteToken(CFDataRef);
static void str(AXUIElementRef e, CFStringRef a, char *o, size_t n){ o[0]=0; CFTypeRef v=NULL; if(AXUIElementCopyAttributeValue(e,a,&v)==kAXErrorSuccess&&v){ if(CFGetTypeID(v)==CFStringGetTypeID()) CFStringGetCString(v,o,n,kCFStringEncodingUTF8); CFRelease(v);} }
static CFDataRef toks[64]; static char labels[64][64]; static int nt=0; static AXUIElementRef lastText=NULL;
static void walk(AXUIElementRef e,int d){ if(d>60) return; char role[64],title[128],val[128]; str(e,kAXRoleAttribute,role,64); str(e,kAXTitleAttribute,title,128); str(e,kAXValueAttribute,val,128);
  if(!strcmp(role,"AXStaticText") && (!strcmp(val,"Alpha")||!strcmp(val,"Beta")||!strcmp(val,"Gamma")||!strcmp(val,"Delta"))) { lastText=e; }
  if(!strcmp(role,"AXButton") && strstr(title,"Delete") && lastText && nt<64){ char lab[64]; str(lastText,kAXValueAttribute,lab,64); snprintf(labels[nt],64,"Delete(%s)",lab); toks[nt++]=_AXUIElementRemoteTokenCreate(e); }
  CFArrayRef k=NULL; if(AXUIElementCopyAttributeValue(e,kAXChildrenAttribute,(CFTypeRef*)&k)==kAXErrorSuccess&&k){ for(CFIndex i=0;i<CFArrayGetCount(k);i++) walk((AXUIElementRef)CFArrayGetValueAtIndex(k,i),d+1); CFRelease(k);} }
static void check(const char*phase){ printf("%s:",phase); for(int i=0;i<nt;i++){ AXUIElementRef e=_AXUIElementCreateWithRemoteToken(toks[i]); char t[128]=""; CFTypeRef v=NULL; AXError err=e?AXUIElementCopyAttributeValue(e,kAXRoleAttribute,&v):-1; if(v)CFRelease(v); if(e) str(e,kAXTitleAttribute,t,128); printf(" %s=%s",labels[i],err==0?"live":"DEAD"); if(e)CFRelease(e);} printf("\n"); fflush(stdout); }
int main(int argc,char**argv){ AXUIElementRef app=AXUIElementCreateApplication(atoi(argv[1])); CFArrayRef w=NULL; AXUIElementCopyAttributeValue(app,kAXWindowsAttribute,(CFTypeRef*)&w); if(w){ for(CFIndex i=0;i<CFArrayGetCount(w);i++) walk((AXUIElementRef)CFArrayGetValueAtIndex(w,i),0);} printf("captured %d Delete buttons\n",nt);
  check("t0 captured"); sleep(7); check("t1 after Beta removed"); sleep(6); check("t2 after Gamma text edit"); sleep(6); check("t3 after full re-render"); return 0; }
