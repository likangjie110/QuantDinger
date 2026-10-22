import 'vue'

declare module 'vue' {
  interface GlobalDirectives {
    vLoading: typeof import('element-plus')['ElLoadingDirective']
  }
}

export {}
