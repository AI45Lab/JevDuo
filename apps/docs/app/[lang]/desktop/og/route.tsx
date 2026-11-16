import { renderOgImage } from "./og-image";

export async function GET() {
  return renderOgImage("Desktop\nAutomation for AI");
}
