const BASE = '/api/agent1'

function authHeaders() {
  return {
    'Content-Type': 'application/json',
    // TODO: add Authorization: Bearer <token> once real auth is implemented
  }
}

async function req(method, path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: authHeaders(),
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? res.statusText)
  }
  if (res.status === 204) return null
  return res.json()
}

export const api = {
  // Users
  getMe:               ()           => req('GET',    '/users/me'),

  // Channels
  createChannel:       (body)       => req('POST',   '/channels', body),
  listChannels:        ()           => req('GET',    '/channels'),
  getChannel:          (id)         => req('GET',    `/channels/${id}`),
  updateChannel:       (id, body)   => req('PUT',    `/channels/${id}`, body),
  deleteChannel:       (id)         => req('DELETE', `/channels/${id}`),
  upsertConfig:        (id, body)   => req('PUT',    `/channels/${id}/config`, body),
  replaceLanguages:    (id, body)   => req('PUT',    `/channels/${id}/languages`, body),
  replaceVoices:       (id, body)   => req('PUT',    `/channels/${id}/voices`, body),
  replaceSources:      (id, body)   => req('PUT',    `/channels/${id}/sources`, body),
  upsertTimings:       (id, body)   => req('PUT',    `/channels/${id}/timings`, body),
  saveCredentials:     (id, body)   => req('POST',   `/channels/${id}/credentials`, body),
  verifyCredential:    (id, body)   => req('POST',   `/channels/${id}/verify`, body),
  getReadiness:        (id)         => req('GET',    `/channels/${id}/readiness`),
  activateChannel:     (id)         => req('POST',   `/channels/${id}/activate`),
  deactivateChannel:   (id)         => req('POST',   `/channels/${id}/deactivate`),
  suggestTiming:       (id)         => req('POST',   `/channels/${id}/suggest-timing`),

  // Voices — browse a TTS provider's real voice catalog (paginated, filtered
  // the same way the provider's own platform lets an operator browse it).
  getVoices: (params) => {
    const query = new URLSearchParams(
      Object.fromEntries(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ''))
    )
    return req('GET', `/voices?${query.toString()}`)
  },
  // Some providers (Cartesia) return no static preview sample at all — this
  // is a plain URL (not a fetch helper) meant to be used directly as an
  // <audio>/Audio() src, which live-synthesizes a short preview clip on GET.
  previewVoiceUrl: (provider, voiceId) =>
    `${BASE}/voices/preview?provider=${encodeURIComponent(provider)}&voice_id=${encodeURIComponent(voiceId)}`,

  // AI suggestions
  suggest:             (field, ctx) => req('POST',   '/suggest', { field, context: ctx }),
  researchIdeas:       (body)       => req('POST',   '/research-ideas', body),
}
