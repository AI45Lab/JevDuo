#include <ApplicationServices/ApplicationServices.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
extern CFDataRef _AXUIElementRemoteTokenCreate(AXUIElementRef);
extern AXUIElementRef _AXUIElementCreateWithRemoteToken(CFDataRef);
static int total=0, withtok=0, roundtrip=0, mismatch=0, lens[256];
static void walk(AXUIElementRef e,int depth){
  if(depth>80||total>20000) return; total++;
  CFDataRef t=_AXUIElementRemoteTokenCreate(e);
  if(t){ withtok++; CFIndex n=CFDataGetLength(t); if(n<256) lens[n]++; AXUIElementRef b=_AXUIElementCreateWithRemoteToken(t); if(b){ if(CFEqual(b,e)) roundtrip++; else mismatch++; CFRelease(b);} else mismatch++; CFRelease(t); }
  CFArrayRef k=NULL; if(AXUIElementCopyAttributeValue(e,kAXChildrenAttribute,(CFTypeRef*)&k)==kAXErrorSuccess&&k){ for(CFIndex i=0;i<CFArrayGetCount(k);i++) walk((AXUIElementRef)CFArrayGetValueAtIndex(k,i),depth+1); CFRelease(k);} }
int main(int argc,char**argv){
  AXUIElementRef app=AXUIElementCreateApplication(atoi(argv[1]));
  AXUIElementSetAttributeValue(app,CFSTR("AXEnhancedUserInterface"),kCFBooleanTrue);
  AXUIElementSetAttributeValue(app,CFSTR("AXManualAccessibility"),kCFBooleanTrue);
  walk(app,0);
  CFArrayRef w=NULL; if(AXUIElementCopyAttributeValue(app,kAXWindowsAttribute,(CFTypeRef*)&w)==kAXErrorSuccess&&w){ printf("windows=%ld ",CFArrayGetCount(w)); for(CFIndex i=0;i<CFArrayGetCount(w);i++) walk((AXUIElementRef)CFArrayGetValueAtIndex(w,i),1); CFRelease(w);} else printf("windows=none ");
  printf("elements=%d with_token=%d (%.1f%%) roundtrip_equal=%d mismatch=%d token_sizes:",total,withtok,total?100.0*withtok/total:0,roundtrip,mismatch);
  for(int i=0;i<256;i++) if(lens[i]) printf(" %dB=%d",i,lens[i]); printf("\n"); return 0; }
