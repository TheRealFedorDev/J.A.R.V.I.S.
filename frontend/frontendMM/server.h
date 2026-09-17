#pragma once

#include <QTcpServer>
#include <QTcpSocket>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QMap>
#include <QVector>
#include <QFile>

/*
    Основная идея в том чтобы просто пересылать пакеты по заданным правилам
*/

struct ClientInfo {
    QString name;   // Неофициальное имя
    QString dType;   // Официальный тип
    int protocolVersion;   // Версия протокола, пока ни на что не влияет
};

class Server : public QTcpServer {
    Q_OBJECT

public:
    Server() {
        if (!this->listen(QHostAddress::Any, 6767)) return; // Пытаемся запустить

        loadConfig();
    }

protected:
    void incomingConnection(qintptr socketDescriptor) override {   // Переписываем базовый метод подключения
        QTcpSocket *socket = new QTcpSocket(this);   
        socket->setSocketDescriptor(socketDescriptor);

        connect(socket, &QTcpSocket::readyRead, this, [this, socket]() {
            handleResponse(socket);
        });

        connect(socket, &QTcpSocket::disconnected, this, [this, socket]() {
            disconnectClient(socket);
        });
    }

    void handleResponse(QTcpSocket* socket) {   // Обрабатываем запросики
        QByteArray data = socket->readAll();
        QJsonDocument doc = QJsonDocument::fromJson(data);

        if (doc.isNull()) return;

        QJsonObject obj = doc.object();
        QString type = obj["Type"].toString();

        if (type == "H") {
            if (obj["isEnd"].toBool()) {
                disconnectClient(socket);
            } else {
                addClient(obj, socket);
            }
        }

        else if (type == "P") {
            routeMessage(socket, obj, data);
        }
    }

private:
    QMap<QTcpSocket*, ClientInfo> clients;
    QMap<QString, QVector<QTcpSocket*>> topicSubscribers; 
    QJsonObject config;

    void loadConfig() {
        QFile file("supported.json");
        
        if (!file.open(QIODevice::ReadOnly)) return;
        
        QByteArray fileData = file.readAll();
        file.close();

        QJsonDocument doc = QJsonDocument::fromJson(fileData);
        if (!doc.isNull() && doc.isObject()) {
            config = doc.object();
        }
    }

    void addClient(const QJsonObject& obj, QTcpSocket* socket) {
        QString dType = obj["dType"].toString();
        
        if (!config.contains(dType)) {
            sendError(socket, "Module not supported");
            return;
        }

        clients[socket] = {
            obj["name"].toString(), 
            dType, 
            obj["protocolVersion"].toInt()
        };

        QJsonObject typeConfig = config[dType].toObject();
        QJsonArray subs = typeConfig["sub"].toArray();
        for (QJsonValue subRef : subs) {
            QString topic = subRef.toString();
            topicSubscribers[topic].append(socket);
        }

        QJsonObject reply = {
            {"Type", "H"}, 
            {"isEnd", false}, 
            {"name", obj["name"]}, 
            {"dType", dType}, 
            {"protocolVersion", obj["protocolVersion"]}
        };

        socket->write(QJsonDocument(reply).toJson(QJsonDocument::Compact) + "\n");    // Подтверждение клиента
    }

    void routeMessage(QTcpSocket* senderSocket, const QJsonObject& obj, const QByteArray& rawData) {
        if (!clients.contains(senderSocket)) return;

        QString senderType = clients[senderSocket].dType;
        
        QString eventName = obj["dType"].toString();

        QString topic = senderType + "." + eventName;

        for (QTcpSocket* subSocket : topicSubscribers.value(topic)) {   // Рассылка всем подписанным
            if (subSocket->isOpen()) {
                subSocket->write(rawData + "\n");
            }
        }
    }

    void disconnectClient(QTcpSocket* socket) {   // Удаляем все связи с клиентом
        if (!clients.contains(socket)) return;
        
        for (auto& subs : topicSubscribers) {
            subs.removeAll(socket);
        }
        
        clients.remove(socket);
        socket->deleteLater();
    }

    void sendError(QTcpSocket* socket, const QString& msg) {   // Отправка ошибок
        QJsonObject err = { 
            {"Type", "E"}, 
            {"error", msg} 
        };

        socket->write(QJsonDocument(err).toJson(QJsonDocument::Compact) + "\n");
    }
};
