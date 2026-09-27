// Browser Geolocation API wrapper with clear, user-facing error handling.
export class LocationError extends Error {
  constructor(message, code = "LOCATION_UNKNOWN") {
    super(message);
    this.code = code;
  }
}

/**
 * Get the current position with high accuracy.
 * @returns {Promise<{latitude: number, longitude: number, accuracy: number}>}
 */
export function getCurrentLocation({ timeout = 15000 } = {}) {
  return new Promise((resolve, reject) => {
    if (!("geolocation" in navigator)) {
      reject(new LocationError("Your browser does not support location access.", "UNSUPPORTED"));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (position) =>
        resolve({
          latitude: position.coords.latitude,
          longitude: position.coords.longitude,
          accuracy: position.coords.accuracy,
        }),
      (error) => {
        switch (error.code) {
          case error.PERMISSION_DENIED:
            reject(
              new LocationError(
                "Location access is required to use attendance. Please allow location in your browser.",
                "PERMISSION_DENIED"
              )
            );
            break;
          case error.POSITION_UNAVAILABLE:
            reject(new LocationError("Your location could not be determined.", "UNAVAILABLE"));
            break;
          case error.TIMEOUT:
            reject(new LocationError("Location request timed out. Please try again.", "TIMEOUT"));
            break;
          default:
            reject(new LocationError("Could not get your location.", "UNKNOWN"));
        }
      },
      { enableHighAccuracy: true, timeout, maximumAge: 0 }
    );
  });
}
