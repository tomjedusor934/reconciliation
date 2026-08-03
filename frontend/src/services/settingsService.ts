import api from '@/api/axios'

const resource = '/settings'

export default {
  getAppName() {
    return api.get(`${resource}/app-name`)
  },
  updateAppName(data: any) {
    return api.put(`${resource}/app-name`, data);
  },
  getAppIcon() {
    return api.get(`${resource}/app-icon`)
  },
  updateAppIcon(data: { value: string }) {
    return api.put(`${resource}/app-icon`, data);
  },
  getPasswordSettings() {
    return api.get(`${resource}/password-settings`)
  },
  updatePasswordSettings(data: any) {
    return api.put(`${resource}/password-settings`, data);
  },
  validatePassword(password: string) {
    return api.post(`${resource}/validate-password`, { password })
  },
}
