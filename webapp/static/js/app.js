document.addEventListener('DOMContentLoaded', function() {
    function escapeHTML(text) {
        return text
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function animateText(element, htmlContent) {
        const tokenRegex = /(<[^>]+>|[^<]+)/g;
        const tokens = htmlContent.match(tokenRegex) || [htmlContent];
        let currentTokenIdx = 0;
        let currentHtml = '';
        
        element.innerHTML = '';
        
        function typeNext() {
            if (currentTokenIdx >= tokens.length) {
                element.innerHTML = htmlContent;
                return;
            }
            
            const token = tokens[currentTokenIdx];
            if (token.startsWith('<')) {
                currentHtml += token;
                element.innerHTML = currentHtml;
                currentTokenIdx++;
                typeNext();
            } else {
                const words = token.split(/(\s+)/);
                let wordIdx = 0;
                
                function typeWord() {
                    if (wordIdx >= words.length) {
                        currentTokenIdx++;
                        setTimeout(typeNext, 10);
                        return;
                    }
                    currentHtml += words[wordIdx];
                    element.innerHTML = currentHtml;
                    
                    const chatContainer = document.getElementById('chat-messages');
                    if (chatContainer) {
                        chatContainer.scrollTop = chatContainer.scrollHeight;
                    }
                    wordIdx++;
                    setTimeout(typeWord, 25);
                }
                typeWord();
            }
        }
        typeNext();
    }

    function typewriter(element) {
        const isTest = window.playwright || window.navigator.webdriver || document.documentElement.dataset.test === 'true';
        if (isTest) {
            element.style.opacity = '1';
            element.style.transform = 'translateY(0)';
            const chatContainer = document.getElementById('chat-messages');
            if (chatContainer) {
                chatContainer.scrollTop = chatContainer.scrollHeight;
            }
            return;
        }

        element.style.opacity = '0';
        element.style.transform = 'translateY(6px)';
        element.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
        
        element.offsetHeight; // force reflow
        element.style.opacity = '1';
        element.style.transform = 'translateY(0)';
        
        const paragraphs = element.querySelectorAll('p, li, h1, h2, h3');
        if (paragraphs.length === 0) {
            animateText(element, element.innerHTML);
        } else {
            paragraphs.forEach((p, idx) => {
                const html = p.innerHTML;
                p.innerHTML = '';
                setTimeout(() => {
                    animateText(p, html);
                }, idx * 250);
            });
        }
    }

    // Capture HTMX request start
    document.body.addEventListener('htmx:configRequest', function(event) {
        if (event.detail.elt && event.detail.elt.classList.contains('chat-input')) {
            const input = event.detail.elt.querySelector('input[name="message"]');
            const messageText = input ? input.value.trim() : '';
            const container = document.getElementById('chat-messages');
            
            if (container && messageText) {
                // Append User Bubble
                const userBubble = document.createElement('div');
                userBubble.className = 'chat-message user temp-msg';
                userBubble.innerHTML = `<p>${escapeHTML(messageText)}</p>`;
                container.appendChild(userBubble);
                
                // Append Thinking Bubble
                const thinkingBubble = document.createElement('div');
                thinkingBubble.className = 'chat-message thinking temp-msg';
                thinkingBubble.innerHTML = `<div class="thinking-dots"><span></span><span></span><span></span></div>`;
                container.appendChild(thinkingBubble);
                
                container.scrollTop = container.scrollHeight;
            }
        }
    });

    // Remove temporary bubbles before swapping server content
    document.body.addEventListener('htmx:beforeSwap', function(event) {
        if (event.detail.target.id === 'chat-messages') {
            const temps = event.detail.target.querySelectorAll('.temp-msg');
            temps.forEach(el => el.remove());
        }
    });

    // Typewriter effect on new assistant message after swap
    document.body.addEventListener('htmx:afterSwap', function(event) {
        if (event.detail.target.id === 'chat-messages') {
            const assistantBubbles = event.detail.target.querySelectorAll('.chat-message.assistant');
            if (assistantBubbles.length > 0) {
                const lastAssistantBubble = assistantBubbles[assistantBubbles.length - 1];
                if (!lastAssistantBubble.dataset.animated) {
                    lastAssistantBubble.dataset.animated = 'true';
                    typewriter(lastAssistantBubble);
                }
            } else {
                event.detail.target.scrollTop = event.detail.target.scrollHeight;
            }
        }
    });

    document.body.addEventListener("click", function(event) {
        const anchor = event.target.closest("a");
        if (anchor && anchor.href && anchor.href.includes("/authorize")) {
            event.preventDefault();
            window.open(anchor.href, "Authorize", "width=600,height=600");
        }
    });

    window.addEventListener("message", function(event) {
        if (event.data === "authorized") {
            const form = document.querySelector(".chat-input");
            if (form) {
                const input = form.querySelector("input[name='message']");
                if (input) {
                    input.value = window.CHAT_AUTHORIZED_MSG || "Authorized. Please check.";
                    form.requestSubmit();
                }
            }
        }
    });
});
