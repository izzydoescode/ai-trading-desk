import {
  barTime,
  clockTime,
  direction,
  directionGlyph,
  MISSING,
  money,
  percent,
  plainReason,
  price,
  shortDate,
  signedMoney,
  signedPercent,
  units,
} from "./format";

describe("formatting", () => {
  it("shows missing values as a dash, never as zero", () => {
    for (const format of [money, signedMoney, price, percent, signedPercent, units, barTime, shortDate]) {
      expect(format(null as never)).toBe(MISSING);
      expect(format(undefined as never)).toBe(MISSING);
    }
    expect(barTime("not a date")).toBe(MISSING);
    expect(shortDate("not a date")).toBe(MISSING);
  });

  it("formats money and signed money", () => {
    expect(money(103412.6)).toBe("$103,412.60");
    expect(signedMoney(3412.6)).toBe("+$3,412.60");
    expect(signedMoney(-932.64)).toBe("-$932.64");
    expect(signedMoney(0)).toBe("+$0.00");
  });

  it("formats prices, percentages and units", () => {
    expect(price(352.776)).toBe("352.78");
    expect(price(57.04, 1)).toBe("57.0");
    expect(percent(0.0125)).toBe("1.25%");
    expect(percent(0.4545, 0)).toBe("45%");
    expect(signedPercent(0.0341)).toBe("+3.41%");
    expect(signedPercent(-0.0126)).toBe("-1.26%");
    expect(units(171)).toBe("171");
    expect(units(35.714285)).toBe("35.7143");
  });

  it("gives direction a word and a glyph", () => {
    expect([direction(1), direction(-1), direction(0), direction(null)]).toEqual([
      "up",
      "down",
      "flat",
      "flat",
    ]);
    expect([directionGlyph(1), directionGlyph(-1), directionGlyph(0)]).toEqual(["▲", "▼", "·"]);
  });

  it("formats bar times in UTC and tidies machine reasons", () => {
    expect(barTime("2025-03-13T21:00:00Z")).toBe("2025-03-13 21:00 UTC");
    expect(shortDate("2025-03-13T21:00:00Z")).toBe("03-13");
    expect(clockTime("2025-03-13T21:00:05Z")).toBe("21:00:05 UTC");
    expect(clockTime(null)).toBe(MISSING);
    expect(clockTime("not a time")).toBe(MISSING);
    expect(plainReason("queued_for_next_open")).toBe("queued for next open");
  });
});
