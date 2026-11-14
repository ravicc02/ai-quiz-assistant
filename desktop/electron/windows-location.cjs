const { execFile } = require("node:child_process");
const path = require("node:path");

const WINDOWS_LOCATION_SCRIPT = String.raw`
$ErrorActionPreference = "Stop"
[void][Windows.Devices.Geolocation.Geolocator,Windows.Devices.Geolocation,ContentType=WindowsRuntime]
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$locator = [Windows.Devices.Geolocation.Geolocator]::new()
$operation = $locator.GetGeopositionAsync()
$method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
  Where-Object {
    $_.Name -eq "AsTask" -and
    $_.IsGenericMethodDefinition -and
    $_.GetParameters().Count -eq 1
  } |
  Select-Object -First 1
if (-not $method) { throw "Windows location adapter unavailable" }
$task = $method.MakeGenericMethod(
  [Windows.Devices.Geolocation.Geoposition]
).Invoke($null, @($operation))
if (-not $task.Wait(15000)) { throw "Windows location request timed out" }
$position = $task.Result
[ordered]@{
  latitude = $position.Coordinate.Point.Position.Latitude
  longitude = $position.Coordinate.Point.Position.Longitude
  accuracy = $position.Coordinate.Accuracy
} | ConvertTo-Json -Compress
`;

function parseWindowsLocation(stdout) {
  let payload;
  try {
    payload = JSON.parse(String(stdout || "").replace(/^\uFEFF/, "").trim());
  } catch {
    throw new Error("Windows 返回的位置格式无效");
  }
  const latitude = Number(payload?.latitude);
  const longitude = Number(payload?.longitude);
  const accuracy = Number(payload?.accuracy);
  if (
    !Number.isFinite(latitude)
    || !Number.isFinite(longitude)
    || Math.abs(latitude) > 90
    || Math.abs(longitude) > 180
    || !Number.isFinite(accuracy)
    || accuracy < 0
  ) {
    throw new Error("Windows 返回的位置坐标无效");
  }
  return { latitude, longitude, accuracy, source: "windows" };
}

function windowsPowerShellPath() {
  const root = process.env.SystemRoot || process.env.WINDIR || "C:\\Windows";
  return path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
}

function readWindowsLocation({ execute = execFile, timeoutMs = 18000 } = {}) {
  return new Promise((resolve, reject) => {
    execute(
      windowsPowerShellPath(),
      ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_LOCATION_SCRIPT],
      {
        encoding: "utf8",
        maxBuffer: 16 * 1024,
        timeout: timeoutMs,
        windowsHide: true,
      },
      (error, stdout) => {
        if (error) {
          reject(new Error("Windows 定位服务不可用"));
          return;
        }
        try {
          resolve(parseWindowsLocation(stdout));
        } catch (cause) {
          reject(cause);
        }
      },
    );
  });
}

module.exports = { parseWindowsLocation, readWindowsLocation };
