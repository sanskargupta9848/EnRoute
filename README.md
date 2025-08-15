# 🧠 NerdCrawler

![Beta Badge](https://img.shields.io/badge/Beta-V1.1-green?logoColor=%23FFFFFF&labelColor=%23000000&color=%233cdb00)
![Website Status](https://img.shields.io/website?url=https%3A%2F%2Fap.projectkryptos.xyz%2F)

![NerdCrawler Screenshot](image.png)

**NerdCrawler** is a fully self-hosted search engine built completely from scratch — backend, frontend, and crawler.  
No reliance on third-party APIs or search engines — just raw crawling and indexing power.

🔗 **Live Demo:** [ap.projectkryptos.xyz](https://ap.projectkryptos.xyz)  
📈 **Uptime:** [Kuma Uptime Monitor](https://kuma.projectkryptos.xyz/status/projectkryptos)

---

## 🚀 Features

- 🌐 Self-hosted backend powered by **Python Flask**
- ⚛️ Frontend built with **React + TailwindCSS**
- 🧭 Custom Python crawler indexing live websites
- 🗃️ Database integration planned for future updates
- 🎨 Modern UI with theming support

---

## 🛠 Tech Stack

| Layer     | Technology                 |
|-----------|----------------------------|
| Frontend  | React, TailwindCSS, Vite   |
| Backend   | Flask (Python)             |
| Crawler   | Custom Python system       |
| Hosting   | 100% self-hosted           |

---

## ⚙️ Getting Started

To run NerdCrawler locally:

1. **Clone the repository**
   ```bash
   git clone https://github.com/<your-username>/nerdcrawler.git
   cd nerdcrawler
   ```

2. **Install dependencies**  
   - Backend:  
     ```bash
     pip install -r requirements.txt
     ```
   - Frontend:  
     ```bash
     cd frontend
     npm install
     ```

3. **Create `config.py` in the backend directory**  
   Use the provided `config.example.py` as a reference.

4. **Run the application**
   - Start backend:
     ```bash
     python app.py
     ```
   - Start frontend:
     ```bash
     npm run dev
     ```
   - Run crawler:
     ```bash
     python main.py
     ```

---

## 📁 Example `config.py`

```python
# Example configuration file
PORT = 5000
CRAWLER_SETTINGS = {
    "max_depth": 3,
    "user_agent": "NerdCrawlerBot/1.0"
}
# Add API keys or DB credentials if required
```

---

## 🧪 Status

**Version:** 1.1 Beta  
✅ Fully working crawler & search engine  
✅ Functional frontend with theming  
🔄 Continuous updates & improvements coming soon

---

## 🤝 Contributing

Contributions are welcome!  
- Fork the repository  
- Create a feature branch  
- Submit a pull request  

You can also open issues for bugs or suggestions.

---

## 📜 License

This project is licensed under the **MIT License**.  
See the [LICENSE](LICENSE) file for details.
