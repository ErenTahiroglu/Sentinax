// analyze/index.ts
import { serve } from "https://deno.land/std@0.168.0/http/server.ts"
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.39.7"

const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type, x-gemini-key',
}

serve(async (req) => {
  // 1. Handle Preflight
  if (req.method === 'OPTIONS') {
    return new Response('ok', { headers: corsHeaders })
  }

  try {
    const supabaseClient = createClient(
      Deno.env.get('SUPABASE_URL') ?? '',
      Deno.env.get('SUPABASE_ANON_KEY') ?? '',
      { global: { headers: { Authorization: req.headers.get('Authorization')! } } }
    )

    // --- 2. BYOK Auth Logic ---
    let geminiApiKey = req.headers.get('x-gemini-key')
    let userIdentifier = req.headers.get('x-forwarded-for') || 'anonymous'

    if (!geminiApiKey) {
      // Check if user is authenticated via JWT
      const { data: { user } } = await supabaseClient.auth.getUser()
      if (user) {
        userIdentifier = user.id
        const { data: decryptedKey, error: vaultError } = await supabaseClient.rpc('get_user_api_key')
        if (vaultError || !decryptedKey) {
          throw new Error('API Key not found in Vault. Please configure your key.')
        }
        geminiApiKey = decryptedKey
      }
    }

    if (!geminiApiKey) {
      return new Response(JSON.stringify({ error: 'Missing Gemini API Key' }), {
        status: 401,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' }
      })
    }

    // --- 3. Distributed Rate Limiting (15 RPM) ---
    const { data: rateLimitPassed, error: rateError } = await supabaseClient.rpc('consume_rate_limit', {
      p_identifier: userIdentifier
    })
    
    if (rateError || !rateLimitPassed) {
      return new Response(JSON.stringify({ error: 'Rate limit exceeded (15 RPM)' }), {
        status: 429,
        headers: { ...corsHeaders, 'Content-Type': 'application/json', 'Retry-After': '60' }
      })
    }

    // --- 4. Fetch Allowed Assets (Latency Optimization: Pre-fetch) ---
    const { data: allowedAssets } = await supabaseClient
      .from('allowed_assets')
      .select('symbol')
      .eq('is_active', true)
    
    const allowedSet = new Set(allowedAssets?.map(a => a.symbol.toUpperCase()) || [])

    // --- 5. PII Sanitization & Gemini API Call ---
    const body = await req.json()
    const { ml_scores, news_context, user_risk_profile } = body

    // Sanitization: Strip PII from user profile
    const sanitizedProfile = {
      risk_tolerance: user_risk_profile?.risk_tolerance || 'medium',
      investment_goal: user_risk_profile?.investment_goal || 'growth'
    }

    const systemInstruction = "Sen eğitimsel bir finans analiz motorusun. ASLA Al/Sat/Tut tavsiyesi verme. Veride olmayan hiçbir şirketi veya olayı uydurma. Yanıtının sonuna mutlaka yasal uyarı (YTD) ekle."
    
    const prompt = `
      ML Skorları: ${JSON.stringify(ml_scores)}
      Haber Özeti: ${JSON.stringify(news_context)}
      Kullanıcı Risk Profili: ${JSON.stringify(sanitizedProfile)}
      
      Yukarıdaki verileri analiz et ve yapılandırılmış JSON formatında yanıt dön.
    `

    // Gemini 2.5 Flash API Call (Structured Output)
    const geminiUrl = `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=${geminiApiKey}`
    
    const responseSchema = {
      type: "object",
      properties: {
        analysis: { type: "string" },
        tickers_mentioned: {
          type: "array",
          items: { type: "string" }
        },
        disclaimer: { type: "string" }
      },
      required: ["analysis", "tickers_mentioned", "disclaimer"]
    }

    const geminiRes = await fetch(geminiUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        contents: [{ parts: [{ text: prompt }] }],
        systemInstruction: { parts: [{ text: systemInstruction }] },
        generationConfig: {
          response_mime_type: "application/json",
          response_schema: responseSchema
        }
      })
    })

    if (geminiRes.status === 429) {
      console.error(`[TELEMETRY] Gemini Quota Exceeded for User/IP: ${userIdentifier}. Prompt tokens may have exceeded Free Tier limits.`)
      return new Response(JSON.stringify({ error: 'Gemini Quota Exceeded (429)' }), {
        status: 429,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' }
      })
    }
    
    if (!geminiRes.ok) {
      const errorText = await geminiRes.text()
      console.error(`[TELEMETRY] Gemini API Error (Status ${geminiRes.status}) for User/IP: ${userIdentifier}. Payload: ${errorText}`)
      throw new Error(`Gemini API returned status ${geminiRes.status}`)
    }

    const geminiData = await geminiRes.json()
    const output = JSON.parse(geminiData.candidates[0].content.parts[0].text)

    // --- 6. Zero-Latency Hallucination Filter ---
    const mentionedTickers = output.tickers_mentioned || []
    for (const ticker of mentionedTickers) {
      if (!allowedSet.has(ticker.toUpperCase().replace('.IS', ''))) {
        console.error(`[TELEMETRY] Hallucination Filter Triggered for User/IP: ${userIdentifier}. Disallowed Ticker: ${ticker}. Raw LLM Output: ${JSON.stringify(output)}`)
        return new Response(JSON.stringify({ 
          error: 'Halüsinasyon İhlali: İzin verilmeyen varlık algılandı.',
          ticker: ticker 
        }), {
          status: 400,
          headers: { ...corsHeaders, 'Content-Type': 'application/json' }
        })
      }
    }

    return new Response(JSON.stringify(output), {
      headers: { ...corsHeaders, 'Content-Type': 'application/json' }
    })

  } catch (err: any) {
    console.error(`[TELEMETRY] Edge Function Crash - ${err.message}\nStack: ${err.stack}`)
    return new Response(JSON.stringify({ error: err.message }), {
      status: 500,
      headers: { ...corsHeaders, 'Content-Type': 'application/json' }
    })
  }
})
