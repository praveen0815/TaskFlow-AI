const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

export interface HealthResponse {
  status: "ok";
}

function isHealthResponse(value: unknown): value is HealthResponse {
  return (
    typeof value === "object" &&
    value !== null &&
    "status" in value &&
    value.status === "ok"
  );
}

export async function getApiHealth(): Promise<HealthResponse> {
  const response = await fetch(`${apiBaseUrl}/health/live`);
  if (!response.ok) {
    throw new Error(`API health request failed (${response.status}).`);
  }
  const payload: unknown = await response.json();
  if (!isHealthResponse(payload)) {
    throw new Error("API health response has an unexpected shape.");
  }
  return payload;
}
