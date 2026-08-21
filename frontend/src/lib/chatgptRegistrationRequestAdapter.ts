import {
  CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
  CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
  type ChatGPTRegistrationMode,
} from '@/lib/chatgptRegistrationMode'
import {
  DEFAULT_CHATGPT_REGISTER_FLOW,
  normalizeChatGPTRegisterFlow,
  type ChatGPTRegisterFlow,
} from '@/lib/chatgptRegisterFlow'

type RegistrationExtra = Record<string, unknown>

export interface ChatGPTRegistrationRequestAdapter {
  readonly mode: ChatGPTRegistrationMode
  readonly registerFlow: ChatGPTRegisterFlow
  extendExtra(extra: RegistrationExtra): RegistrationExtra
}

abstract class BaseChatGPTRegistrationRequestAdapter
  implements ChatGPTRegistrationRequestAdapter
{
  abstract readonly mode: ChatGPTRegistrationMode
  abstract readonly hasRefreshTokenSolution: boolean
  readonly registerFlow: ChatGPTRegisterFlow

  constructor(registerFlow: ChatGPTRegisterFlow) {
    this.registerFlow = registerFlow
  }

  extendExtra(extra: RegistrationExtra): RegistrationExtra {
    return {
      ...extra,
      chatgpt_registration_mode: this.mode,
      chatgpt_has_refresh_token_solution: this.hasRefreshTokenSolution,
      chatgpt_register_flow: this.registerFlow,
    }
  }
}

class RefreshTokenChatGPTRegistrationRequestAdapter extends BaseChatGPTRegistrationRequestAdapter {
  readonly mode = CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN
  readonly hasRefreshTokenSolution = true
}

class AccessTokenOnlyChatGPTRegistrationRequestAdapter extends BaseChatGPTRegistrationRequestAdapter {
  readonly mode = CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY
  readonly hasRefreshTokenSolution = false
}

export function buildChatGPTRegistrationRequestAdapter(
  platform: string | undefined,
  mode: ChatGPTRegistrationMode,
  registerFlow: ChatGPTRegisterFlow = DEFAULT_CHATGPT_REGISTER_FLOW,
): ChatGPTRegistrationRequestAdapter | null {
  if (platform !== 'chatgpt') return null

  const flow = normalizeChatGPTRegisterFlow(registerFlow)
  if (mode === CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY) {
    return new AccessTokenOnlyChatGPTRegistrationRequestAdapter(flow)
  }

  return new RefreshTokenChatGPTRegistrationRequestAdapter(flow)
}
