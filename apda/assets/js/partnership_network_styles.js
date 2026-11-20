export function attendanceRadius(tournaments, maximum) {
  return 3 + 5 * Math.sqrt(tournaments / Math.max(1, maximum));
}

export function partnershipWidth(tournaments, maximum = 20) {
  const strength =
    Math.log(Math.max(1, tournaments)) / Math.log(Math.max(2, maximum));
  return 0.45 + 6.15 * strength ** 1.2;
}
