// Credential field definitions per platform.
// When real verification is implemented, these map directly to the
// credentials dict stored encrypted in channel_platforms.credentials_encrypted.
export const CREDENTIAL_FIELDS = {
  youtube: [
    { key: 'client_id',     label: 'Client ID',      type: 'text',     placeholder: 'Google Cloud Console client ID' },
    { key: 'client_secret', label: 'Client Secret',  type: 'password', placeholder: '' },
    { key: 'access_token',  label: 'Access Token',   type: 'password', placeholder: '' },
    { key: 'refresh_token', label: 'Refresh Token',  type: 'password', placeholder: '' },
  ],
  tiktok: [
    { key: 'client_key',    label: 'Client Key',    type: 'text',     placeholder: '' },
    { key: 'client_secret', label: 'Client Secret', type: 'password', placeholder: '' },
    { key: 'access_token',  label: 'Access Token',  type: 'password', placeholder: '' },
    { key: 'open_id',       label: 'Open ID',       type: 'text',     placeholder: '' },
  ],
  instagram: [
    { key: 'access_token',                  label: 'Access Token',          type: 'password', placeholder: 'Long-lived page access token' },
    { key: 'instagram_business_account_id', label: 'Instagram Business ID', type: 'text',     placeholder: '' },
  ],
  facebook: [
    { key: 'access_token', label: 'Page Access Token', type: 'password', placeholder: '' },
    { key: 'page_id',      label: 'Page ID',           type: 'text',     placeholder: '' },
  ],
}
