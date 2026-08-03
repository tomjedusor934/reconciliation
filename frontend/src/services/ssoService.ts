import api from '@/api/axios'
import type {
  SSOPreset,
  SSOProvider,
  SSOProviderInput,
  SSOPublicConfig,
  SSOSettings,
} from '@/types'

const resource = '/sso'

export default {
  // Public — used by the LoginView
  getPublicConfig() {
    return api.get<SSOPublicConfig>(`${resource}/providers`)
  },
  loginWith(providerName: string) {
    // Full-page redirect to the IdP through the backend.
    const apiBase = (import.meta as any).env.VITE_API_URL || '/api/v1'
    window.location.href = `${apiBase}${resource}/login/${encodeURIComponent(providerName)}`
  },

  // Admin
  getSettings() {
    return api.get<SSOSettings>(`${resource}/admin/settings`)
  },
  updateSettings(payload: Partial<SSOSettings>) {
    return api.put<SSOSettings>(`${resource}/admin/settings`, payload)
  },
  getPresets() {
    return api.get<SSOPreset[]>(`${resource}/admin/presets`)
  },
  getProviders() {
    return api.get<SSOProvider[]>(`${resource}/admin/providers`)
  },
  createProvider(payload: SSOProviderInput) {
    return api.post<SSOProvider>(`${resource}/admin/providers`, payload)
  },
  updateProvider(id: number, payload: Partial<SSOProviderInput>) {
    return api.put<SSOProvider>(`${resource}/admin/providers/${id}`, payload)
  },
  deleteProvider(id: number) {
    return api.delete(`${resource}/admin/providers/${id}`)
  },
}
