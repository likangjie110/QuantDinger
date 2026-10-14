import zhCN from './zh-CN'

const messages = {
  'zh-CN': zhCN
}

let currentLocale = 'zh-CN'

export function useI18n() {
  const t = (key) => {
    const keys = key.split('.')
    let value = messages[currentLocale]

    for (const k of keys) {
      value = value?.[k]
      if (value === undefined) break
    }

    return value || key
  }

  return { t }
}

export function setLocale(locale) {
  currentLocale = locale
}
