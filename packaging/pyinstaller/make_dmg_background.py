"""Draw the DMG installer window background (no third-party deps — AppKit via pyobjc).

  python packaging/pyinstaller/make_dmg_background.py out.png

640x400 points, matching the dashboard's dark HUD look. Icon slots sit at x=160
and x=480; build.sh positions the two icons to line up with the arrow.
"""
import sys

from AppKit import (NSBitmapImageRep, NSBezierPath, NSColor, NSFont,
                    NSGradient, NSGraphicsContext, NSMakeRect, NSMakePoint,
                    NSMutableDictionary, NSString, NSPNGFileType,
                    NSForegroundColorAttributeName, NSFontAttributeName)

W, H = 640, 400
ICON_Y = 400 - 205          # AppKit origin is bottom-left; Finder's is top-left
LEFT_X, RIGHT_X = 160, 480


def rgb(r, g, b, a=1.0):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255, g / 255, b / 255, a)


def draw(path):
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, W, H, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0)
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)

    # Diagonal wash: cyan-tinted navy -> deep navy -> purple-tinted navy. Drawn across
    # the whole canvas, so there is no path edge for a radial falloff to reveal.
    NSGradient.alloc().initWithColors_([rgb(14, 32, 46), rgb(10, 15, 28), rgb(28, 16, 44)]) \
        .drawInRect_angle_(NSMakeRect(0, 0, W, H), 200.0)

    # Arrow from the app towards the Applications folder.
    y = ICON_Y
    arrow = NSBezierPath.bezierPath()
    arrow.moveToPoint_(NSMakePoint(LEFT_X + 95, y))
    arrow.lineToPoint_(NSMakePoint(RIGHT_X - 108, y))
    arrow.setLineWidth_(3.0)
    rgb(34, 211, 238, 0.75).setStroke()
    arrow.stroke()
    head = NSBezierPath.bezierPath()
    head.moveToPoint_(NSMakePoint(RIGHT_X - 95, y))
    head.lineToPoint_(NSMakePoint(RIGHT_X - 116, y + 11))
    head.lineToPoint_(NSMakePoint(RIGHT_X - 116, y - 11))
    head.closePath()
    rgb(34, 211, 238, 0.75).setFill()
    head.fill()

    def text(s, size, colour, cy, weight=NSFont.systemFontOfSize_):
        attrs = NSMutableDictionary.dictionary()
        attrs[NSFontAttributeName] = weight(size)
        attrs[NSForegroundColorAttributeName] = colour
        ns = NSString.stringWithString_(s)
        w = ns.sizeWithAttributes_(attrs).width
        ns.drawAtPoint_withAttributes_(NSMakePoint((W - w) / 2, cy), attrs)

    text("Orrin", 30, rgb(230, 236, 246), H - 78, NSFont.boldSystemFontOfSize_)
    text("Drag Orrin into your Applications folder", 14, rgb(157, 176, 200), H - 108)
    text("Voice + screen for the Claude you already run", 12, rgb(125, 138, 163), 34)

    NSGraphicsContext.restoreGraphicsState()
    rep.representationUsingType_properties_(NSPNGFileType, None).writeToFile_atomically_(path, True)
    print(f"wrote {path} ({W}x{H})")


if __name__ == "__main__":
    draw(sys.argv[1] if len(sys.argv) > 1 else "dmg-background.png")
