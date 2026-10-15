from flask import Flask, render_template

app = Flask(__name__)

@app.route("/")
def dashboard():
    return render_template(
        "index.html",
        files=[],
        folders=[],
        total_files=0,
        total_folders=0
    )

if __name__ == "__main__":
    app.run(debug=True)

